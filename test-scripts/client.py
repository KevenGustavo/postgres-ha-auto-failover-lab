import time
import sys
import psycopg2
from psycopg2 import OperationalError

DB_CONFIG = {
    "dbname": "postgres",
    "user": "postgres",
    "password": "postgres_master_pwd",
    "host": "localhost",
    "port": 5000
}

def get_connection():
    return psycopg2.connect(**DB_CONFIG)

def setup_database():
    """Garante que a tabela de testes existe no Leader atual"""
    try:
        conn = get_connection()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_transactions (
                    id SERIAL PRIMARY KEY,
                    client_id INT,
                    amount DECIMAL(10,2),
                    processed_by_node INET,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        conn.close()
    except Exception as e:
        print(f"\033[91m[!] Initial DB setup failed: {e}\033[0m")
        sys.exit(1)

def run_traffic_generator():
    setup_database()
    print("\033[94m[i] Traffic Generator online. Hitting HAProxy at localhost:5000...\033[0m")
    
    transaction_counter = 1000
    
    while True:
        transaction_counter += 1
        client_id = (transaction_counter % 50) + 1
        amount = 150.50
        
        success = False
        attempts = 0
        
        while not success:
            try:
                attempts += 1
                conn = get_connection()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO app_transactions (client_id, amount, processed_by_node) 
                        VALUES (%s, %s, inet_server_addr()) 
                        RETURNING id, processed_by_node;
                    """, (client_id, amount))
                    
                    tx_id, node_ip = cur.fetchone()
                    conn.commit()
                    
                    print(f"\033[92m[SUCCESS] Tx #{tx_id} | Amount: ${amount} | Written to Node IP: {node_ip}\033[0m")
                    success = True
                    
                conn.close()
                time.sleep(0.4)
                
            except OperationalError as e:
                wait_time = 1.5 * attempts
                print(f"\033[93m[WARN] Database connection dropped! HAProxy failing over... Retrying in {wait_time}s (Attempt {attempts}/5)\033[0m")
                time.sleep(wait_time)
                
                if attempts >= 5:
                    print("\033[91m[FATAL] Cluster completely offline. RTO exceeded.\033[0m")
                    sys.exit(1)

if __name__ == "__main__":
    run_traffic_generator()