#!/usr/bin/env bash
# Start a disposable PostgreSQL instance inside the worker container.
#
# Usage: orch-postgres [database]
#
# The data directory lives under /tmp, so it is recreated for every worker
# container. Within one attempt the helper is idempotent: setup runs before the
# agent and again before the verification gate.
set -euo pipefail

DB_NAME="${1:-${ORCH_POSTGRES_DB:-orch_test}}"
PGDATA="${ORCH_PGDATA:-/tmp/orch-pg}"
PGPORT="${ORCH_PGPORT:-5432}"
PGUSER="${ORCH_PGUSER:-postgres}"
PGHOST="127.0.0.1"

# Track whatever major the image installed instead of hardcoding it, so the
# helper keeps working when the Dockerfile bumps PostgreSQL (16 -> 17).
PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
export PATH="${PG_BIN}:${PATH}"

if [ ! -s "${PGDATA}/PG_VERSION" ]; then
    rm -rf "${PGDATA}"
    initdb -D "${PGDATA}" -U "${PGUSER}" --auth=trust \
        --encoding=UTF8 --locale=C >/tmp/orch-pg-init.log 2>&1
fi

if ! pg_isready -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" >/dev/null 2>&1; then
    pg_ctl -D "${PGDATA}" -l /tmp/orch-pg.log \
        -o "-h ${PGHOST} -p ${PGPORT} -k /tmp" -w start >/dev/null
fi

for _ in $(seq 1 60); do
    if pg_isready -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! pg_isready -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" >/dev/null 2>&1; then
    echo "Postgres did not become ready; see /tmp/orch-pg.log" >&2
    exit 1
fi

if ! psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    createdb -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" "${DB_NAME}"
fi

echo "postgres://${PGUSER}@${PGHOST}:${PGPORT}/${DB_NAME}"
