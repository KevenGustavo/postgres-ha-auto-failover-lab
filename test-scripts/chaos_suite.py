import subprocess
import sys
import threading
import time
import psycopg2

IP_TO_NODE = {
    "172.20.0.21": "pg-node1",
    "172.20.0.22": "pg-node2",
    "172.20.0.23": "pg-node3",
}

DB_CONFIG = {
    "dbname": "postgres",
    "user": "postgres",
    "password": "postgres_master_pwd",
    "host": "localhost",
    "port": 5000,
    "connect_timeout": 3
}

class EnterpriseSREChaosSuite:
    def __init__(self):
        self.running = True
        self.records_log = [] 
        self.current_phase = "0_PRE_FLIGHT"
        self.chaos_strike_time = None
        self.first_success_after_chaos = None
        self.initial_leader_ip = None
        self.new_leader_ip = None
        self.last_tx_snapshot = None
        self.lock = threading.Lock()

    def execute_shell(self, command):
        return subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()

    def run_preflight_check(self):
        sys.stdout.write("[INFO] Executing Pre-Flight Gateway & Engine verification...\n")
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            cur = conn.cursor()
            cur.execute("SELECT 1;")
            cur.close()
            conn.close()
            sys.stdout.write("[PASS] HAProxy Gateway and PostgreSQL Primary reachable.\n")
        except Exception as e:
            sys.stdout.write(f"\n[FATAL] Pre-flight check failed. Cluster is down: {str(e).split(chr(10))[0]}\n")
            sys.exit(1)

    def setup_ledger_table(self):
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS chaos_ledger;")
            cur.execute("""
                CREATE TABLE chaos_ledger (
                    id SERIAL PRIMARY KEY,
                    client_seq INT,
                    node_processed TEXT,
                    injection_phase TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        conn.close()

    def background_payload_injector(self):
        seq = 0
        while self.running:
            seq += 1
            tx_success = False
            
            while not tx_success and self.running:
                try:
                    conn = psycopg2.connect(**DB_CONFIG)
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO chaos_ledger (client_seq, node_processed, injection_phase)
                            VALUES (%s, inet_server_addr(), %s)
                            RETURNING id, inet_server_addr();
                        """, (seq, self.current_phase))
                        tx_id, node_ip = cur.fetchone()
                        conn.commit()

                    with self.lock:
                        tx_ts = time.time()
                        self.records_log.append((tx_ts, "SUCCESS", tx_id, node_ip, self.current_phase))
                        self.last_tx_snapshot = {"id": tx_id, "ip": node_ip, "seq": seq}
                        
                        if self.chaos_strike_time and not self.first_success_after_chaos:
                            if node_ip != self.initial_leader_ip:
                                self.first_success_after_chaos = tx_ts
                                self.new_leader_ip = node_ip

                    tx_success = True
                    conn.close()
                    time.sleep(0.08)

                except (psycopg2.Error, Exception) as e:
                    with self.lock:
                        err_name = type(e).__name__
                        self.records_log.append((time.time(), "FAIL", seq, err_name, self.current_phase))
                    time.sleep(0.15)

    def tui_ticker(self, seconds, prompt_text):
        """Atualiza o status no terminal na mesma linha via Carriage Return"""
        start_time = time.time()
        while time.time() - start_time < seconds:
            with self.lock:
                ok_cnt = len([r for r in self.records_log if r[1] == "SUCCESS"])
                fail_cnt = len([r for r in self.records_log if r[1] == "FAIL"])
                if self.last_tx_snapshot:
                    curr_ip = self.last_tx_snapshot["ip"]
                    curr_id = self.last_tx_snapshot["id"]
                    status_line = f"[INJECTOR] {prompt_text} | Commits: {ok_cnt} | Retries: {fail_cnt} | Active IP: {curr_ip} (Tx #{curr_id})"
                else:
                    status_line = f"[INJECTOR] {prompt_text} | Commits: {ok_cnt} | Retries: {fail_cnt}"
                
                sys.stdout.write(f"\r{status_line:<95}")
                sys.stdout.flush()
            time.sleep(0.15)
        sys.stdout.write("\n")

    def audit_physical_storage(self, milestone_title):
        """Bypassa o HAProxy e faz SELECT count(*) direto no disco de cada container"""
        sys.stdout.write(f"\n+---------------------------------------------------------------------------------------------+\n")
        sys.stdout.write(f"| STORAGE AUDIT: {milestone_title:<76} |\n")
        sys.stdout.write(f"+-----------+-----------------+------------+--------------------------------------------------+\n")
        sys.stdout.write(f"| NODE      | PHYSICAL IP     | ROWS COUNT | STORAGE REPLICATION ENGINE STATE                 |\n")
        sys.stdout.write(f"+-----------+-----------------+------------+--------------------------------------------------+\n")

        for ip, name in IP_TO_NODE.items():
            try:
                cnt_cmd = f'docker exec {name} psql -U postgres -d postgres -t -A -c "SELECT count(*) FROM chaos_ledger;"'
                rows = self.execute_shell(cnt_cmd)
                
                role_cmd = f'docker exec {name} psql -U postgres -d postgres -t -A -c "SELECT CASE WHEN pg_is_in_recovery() THEN \'Standby Replica (Read-Only)\' ELSE \'Primary Leader (Read-Write)\' END;"'
                role = self.execute_shell(role_cmd)
                
                sys.stdout.write(f"| {name:<9} | {ip:<15} | {rows:<10} | {role:<48} |\n")
            except Exception:
                sys.stdout.write(f"| {name:<9} | {ip:<15} | {'[OFFLINE]':<10} | {'[CONTAINER UNREACHABLE / ENGINE DEAD]':<48} |\n")

        sys.stdout.write(f"+-----------+-----------------+------------+--------------------------------------------------+\n\n")

    def run_acceptance_suite(self):
        sys.stdout.write("\n" + "="*95 + "\n")
        sys.stdout.write("UNCOMPROMISING SRE CHAOS SUITE\n")
        sys.stdout.write("="*95 + "\n\n")

        # 0. Pre-Flight
        self.run_preflight_check()
        self.setup_ledger_table()

        # 1. Baseline
        self.current_phase = "1_BASELINE"
        injector_thread = threading.Thread(target=self.background_payload_injector)
        injector_thread.start()

        self.tui_ticker(4, "Establishing steady-state baseline")
        
        with self.lock:
            self.initial_leader_ip = [r for r in self.records_log if r[1] == "SUCCESS"][-1][3]
            initial_target_name = IP_TO_NODE.get(self.initial_leader_ip, "UNKNOWN")

        sys.stdout.write(f"[PASS] Steady-state locked. Primary Target: [{initial_target_name}] ({self.initial_leader_ip})\n")
        self.audit_physical_storage("MILESTONE 1: INITIAL STEADY STATE")

        # 2. Chaos Injection
        self.current_phase = "2_DURING_CHAOS"
        sys.stdout.write(f"[WARN] INJECTING FATAL CHAOS: Executing 'docker kill {initial_target_name}'...\n")
        self.chaos_strike_time = time.time()
        self.execute_shell(f"docker kill {initial_target_name}")

        watchdog_start = time.time()
        while not self.first_success_after_chaos:
            time.sleep(0.05)
            if time.time() - watchdog_start > 35.0:
                sys.stdout.write("\n[FATAL] Watchdog Timer expired (35s). Cluster failed to failover.\n")
                self.running = False
                sys.exit(1)

        rto = self.first_success_after_chaos - self.chaos_strike_time
        new_leader_name = IP_TO_NODE.get(self.new_leader_ip, "UNKNOWN")
        sys.stdout.write(f"[PASS] Quorum re-elected. HAProxy rerouted to: [{new_leader_name}] ({self.new_leader_ip}) | RTO: {rto:.2f}s\n\n")

        # 3. Orphan Writes Window (Gravando com o nó antigo apagado)
        self.current_phase = "3_ORPHAN_WINDOW"
        self.tui_ticker(5, f"Writing orphan payload exclusively to {new_leader_name}")
        self.audit_physical_storage("MILESTONE 2: POST-FAILOVER (TARGET DEAD)")

        # 4. Self-Healing & Catch-up
        self.current_phase = "4_RECOVERY_CATCHUP"
        sys.stdout.write(f"[INFO] Triggering Self-Healing: Powering ON '{initial_target_name}'...\n")
        self.execute_shell(f"docker start {initial_target_name}")
        
        self.tui_ticker(18, "Awaiting Patroni pg_rewind & WAL catch-up sync")
        self.audit_physical_storage("MILESTONE 3: FINAL RECONCILIATION")

        # Encerramento da artilharia
        self.running = False
        injector_thread.join()

        # 5. Auditoria Matemática de Reconciliação
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chaos_ledger;")
            db_total_rows = cur.fetchone()[0]
            
            cur.execute("SELECT count(*) FROM chaos_ledger WHERE injection_phase = '3_ORPHAN_WINDOW';")
            db_orphan_rows = cur.fetchone()[0]
        conn.close()

        app_ok = len([r for r in self.records_log if r[1] == "SUCCESS"])
        app_fails = len([r for r in self.records_log if r[1] == "FAIL"])
        orphan_app_attempts = len([r for r in self.records_log if r[1] == "SUCCESS" and r[4] == "3_ORPHAN_WINDOW"])

        # Prova física de sincronização
        try:
            sync_check_cmd = f'docker exec {initial_target_name} psql -U postgres -d postgres -t -A -c "SELECT count(*) FROM chaos_ledger WHERE injection_phase = \'3_ORPHAN_WINDOW\';"'
            resurrected_orphan_count = int(self.execute_shell(sync_check_cmd))
        except Exception:
            resurrected_orphan_count = -1

        sys.stdout.write("="*95 + "\n")
        sys.stdout.write("EXECUTIVE SRE MATHEMATICAL AUDIT REPORT\n")
        sys.stdout.write("="*95 + "\n")
        sys.stdout.write(f" [GATEWAY METRICS]\n")
        sys.stdout.write(f"  • Primary Target Assassinated : {initial_target_name} ({self.initial_leader_ip})\n")
        sys.stdout.write(f"  • Cluster Failover RTO        : {rto:.2f} seconds\n")
        sys.stdout.write(f"  • Network Timeouts Caught     : {app_fails} transactions \n\n")
        
        sys.stdout.write(f" [DATA INTEGRITY METRICS (RPO)]\n")
        sys.stdout.write(f"  • App Confirmed Writes        : {app_ok} rows\n")
        sys.stdout.write(f"  • Storage Physical Rows       : {db_total_rows} rows\n")
        sys.stdout.write(f"  • Absolute Cluster RPO        : {app_ok - db_total_rows} rows lost " + ("[PERFECT INTEGRITY]\n\n" if (app_ok - db_total_rows) == 0 else "[BREACHED]\n\n"))
        
        sys.stdout.write(f" [ORPHAN REPLICATION RECONCILIATION]\n")
        sys.stdout.write(f"  • Orphan Window App Writes    : {orphan_app_attempts} rows generated while {initial_target_name} was DEAD\n")
        sys.stdout.write(f"  • Replicated to Resurrected   : {resurrected_orphan_count} rows physically verified in {initial_target_name} disk\n")
        sys.stdout.write("="*95 + "\n")
        
        if (app_ok - db_total_rows) == 0 and resurrected_orphan_count == orphan_app_attempts:
            sys.stdout.write("[CERTIFIED] High Availability and Self-Healing Acceptance test PASSED.\n")
        else:
            sys.stdout.write("[REJECTED] Cluster survived, but data convergence failed verification.\n")

if __name__ == "__main__":
    suite = EnterpriseSREChaosSuite()
    suite.run_acceptance_suite()