#!/bin/bash
# Supervisor for the OpenHost Keycloak app.
#
# Runs three sibling processes and tears the container down if any of
# them dies (podman restarts us via --restart=unless-stopped):
#   1. Postgres 16          (127.0.0.1:5432, data in $OPENHOST_APP_DATA_DIR)
#   2. Keycloak             (127.0.0.1:8081)
#   3. auth_proxy.py        (0.0.0.0:8080, the OpenHost-routed port)
#
# Secrets policy: NOTHING secret is ever written to $OPENHOST_APP_DATA_DIR
# (other apps, e.g. file-browser, can read that directory). The first-boot
# bootstrap admin credentials live only in process environments and the
# container log. The Postgres password is a fixed constant; Postgres only
# listens on loopback inside this container, so it is not a secret.

set -u

DATA_DIR="${OPENHOST_APP_DATA_DIR:?OPENHOST_APP_DATA_DIR must be set}"
PG_DIR="$DATA_DIR/postgres"
PG_BIN="/usr/lib/postgresql/16/bin"
PG_SOCKET_DIR="/var/run/postgresql"

# Loopback-only constant; see "Secrets policy" above and README.md.
KC_DB_PASSWORD_VALUE="keycloak-loopback-only"

echo "[start] OpenHost Keycloak starting (app=${OPENHOST_APP_NAME:-keycloak} zone=${OPENHOST_ZONE_DOMAIN:-unknown})"

# ---------------------------------------------------------------- postgres
mkdir -p "$PG_DIR" "$PG_SOCKET_DIR"
chown postgres:postgres "$PG_DIR" "$PG_SOCKET_DIR"
chmod 700 "$PG_DIR"

if [ ! -s "$PG_DIR/PG_VERSION" ]; then
    echo "[start] Initializing Postgres data directory"
    gosu postgres "$PG_BIN/initdb" \
        --pgdata="$PG_DIR" \
        --encoding=UTF8 \
        --locale=C.UTF-8 \
        --auth-local=peer \
        --auth-host=scram-sha-256 \
        >/dev/null
fi

# All connection-facing settings are passed on the command line so the
# on-disk config never drifts; Postgres is loopback-only by design.
gosu postgres "$PG_BIN/postgres" \
    -D "$PG_DIR" \
    -c listen_addresses=127.0.0.1 \
    -c port=5432 \
    -c unix_socket_directories="$PG_SOCKET_DIR" \
    &
PG_PID=$!

echo "[start] Waiting for Postgres"
pg_ready=0
for _ in $(seq 1 60); do
    if gosu postgres "$PG_BIN/pg_isready" -h "$PG_SOCKET_DIR" -p 5432 -q; then
        pg_ready=1
        break
    fi
    sleep 1
done
if [ "$pg_ready" != 1 ]; then
    echo "[start] FATAL: Postgres did not become ready" >&2
    kill -INT "$PG_PID" 2>/dev/null
    exit 1
fi

psql_super() {
    gosu postgres psql -h "$PG_SOCKET_DIR" -p 5432 -v ON_ERROR_STOP=1 "$@"
}

# Idempotent role + database provisioning.
if ! psql_super -tAc "SELECT 1 FROM pg_roles WHERE rolname='keycloak'" | grep -q 1; then
    psql_super -c "CREATE ROLE keycloak LOGIN" >/dev/null
fi
# (Re)set the password every boot so upgrades of this script take effect.
psql_super -c "ALTER ROLE keycloak WITH LOGIN PASSWORD '$KC_DB_PASSWORD_VALUE'" >/dev/null
if ! psql_super -tAc "SELECT 1 FROM pg_database WHERE datname='keycloak'" | grep -q 1; then
    psql_super -c "CREATE DATABASE keycloak OWNER keycloak" >/dev/null
fi

# "Fresh" means Keycloak has never initialized its schema in this
# database (also covers a first boot that crashed before Keycloak got
# that far, not just a database we created moments ago).
FRESH_DATABASE=0
if ! psql_super -d keycloak -tAc \
    "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='user_entity'" \
    | grep -q 1; then
    FRESH_DATABASE=1
fi

# ---------------------------------------------------------------- keycloak
export KC_DB_URL="jdbc:postgresql://127.0.0.1:5432/keycloak"
export KC_DB_USERNAME="keycloak"
export KC_DB_PASSWORD="$KC_DB_PASSWORD_VALUE"
export KC_HTTP_ENABLED="true"
export KC_HTTP_HOST="127.0.0.1"
export KC_HTTP_PORT="8081"
export KC_HTTP_MANAGEMENT_HOST="127.0.0.1"
export KC_HTTP_MANAGEMENT_PORT="9000"
export KC_PROXY_HEADERS="xforwarded"
export KC_HOSTNAME_STRICT="false"
# Keep the JVM well under the app's 2 GiB cgroup limit; Postgres and the
# proxy share the same container.
export JAVA_OPTS_KC_HEAP="-Xms128m -Xmx1024m"

# First-boot admin bootstrap: on a completely fresh database, create a
# temporary admin with a random password and print the credentials to the
# container log (visible via `podman logs` / the OpenHost dashboard, host
# operators only). Log in at /admin/ (gated behind OpenHost owner auth),
# create permanent named admin accounts for every human who needs access,
# then delete the temporary one. All admin-console logins go through
# Keycloak's own authentication, so admin events are attributable to
# individual users.
if [ "$FRESH_DATABASE" = 1 ]; then
    export KC_BOOTSTRAP_ADMIN_USERNAME="admin"
    KC_BOOTSTRAP_ADMIN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    export KC_BOOTSTRAP_ADMIN_PASSWORD
    echo "[start] ============================================================"
    echo "[start] First boot: creating temporary Keycloak admin"
    echo "[start]   username: $KC_BOOTSTRAP_ADMIN_USERNAME"
    echo "[start]   password: $KC_BOOTSTRAP_ADMIN_PASSWORD"
    echo "[start] Log in at /admin/, create permanent admin users, then"
    echo "[start] delete this temporary account."
    echo "[start] ============================================================"
fi

# -------------------------------------------------------------- auth proxy
# Started before the (slow) Keycloak JVM so OpenHost's readiness probe on
# the routed port is answered immediately.
PROXY_LISTEN_PORT=8080 KC_UPSTREAM_PORT=8081 \
    python3 /app/auth_proxy.py &
PROXY_PID=$!

gosu keycloak /opt/keycloak/bin/kc.sh start --optimized --import-realm &
KC_PID=$!

# ------------------------------------------------------------- supervision
shutdown() {
    echo "[start] Shutting down"
    kill -TERM "$PROXY_PID" "$KC_PID" 2>/dev/null
    # SIGINT = Postgres "fast shutdown".
    kill -INT "$PG_PID" 2>/dev/null
    wait
    exit 0
}
trap shutdown TERM INT

wait -n "$PG_PID" "$KC_PID" "$PROXY_PID"
status=$?
echo "[start] A supervised process exited (status=$status); stopping container" >&2
trap - TERM INT
kill -TERM "$PROXY_PID" "$KC_PID" 2>/dev/null
kill -INT "$PG_PID" 2>/dev/null
wait
exit 1
