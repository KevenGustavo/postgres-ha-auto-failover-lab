# Baseado na imagem oficial estável do Postgres 16
FROM postgres:16

# Instalação das dependências necessárias para o Patroni (Python3 e Pip)
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-dev \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Instalação do Patroni com suporte específico ao driver etcd3 e psycopg2 (driver de conexão do Postgres)
RUN pip3 install --break-system-packages patroni[etcd3] psycopg2-binary

# Criação do diretório de configuração do Patroni com as permissões para o usuário postgres
RUN mkdir -p /etc/patroni && chown postgres:postgres /etc/patroni

# Por segurança e boas práticas, o container não deve rodar como root
USER postgres

# Expõe as portas: 5432 (Postgres) e 8008 (API REST do Patroni)
EXPOSE 5432 8008

# O comando padrão será sobrescrito pelo Docker Compose injetan do as variáveis de ambiente
CMD ["patroni", "/etc/patroni/patroni.yml"]