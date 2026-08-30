# PostgreSQL 16 verification baseline

This contract is test-only. It uses database `sleepagent_replay_test`, the
public placeholder credentials in `.env.test.example`, and loopback port
`127.0.0.1:15432`. Never point these commands at a live database.

## Canonical Compose path

```bash
cp -n .env.test.example .env.test
docker compose --env-file .env.test config --quiet
docker compose --env-file .env.test up -d --build --wait \\
  postgres migrate test-bootstrap

set -a
. ./.env.test
set +a
python -m sleepagent.persistence.migrate check \\
  --database-url-env SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN
python -m pytest -q -m postgres
```

The committed host-port value includes `127.0.0.1`; do not replace it with a
bare port on a shared host. Stop while retaining the dedicated test volume:

```bash
docker compose --env-file .env.test down
```

The following reset is destructive, but only for the Compose project's
dedicated test volume. It is the required preparation before a repeat of the
full PostgreSQL marker because those integration tests intentionally exercise
durable state and are authoritative from a fresh database:

```bash
docker compose --env-file .env.test down --volumes
docker compose --env-file .env.test up -d --build --wait \\
  postgres migrate test-bootstrap
```

## Verified user-owned PostgreSQL 16 fallback

Use this path when Docker is inaccessible but a user-owned PostgreSQL 16
distribution is available. Set `SLEEPAGENT_PG16_BIN` to a directory containing
`postgres`, `initdb`, `pg_ctl`, `psql`, `createdb`, and `dropdb`. The path
shown below is the installation verified by G2B-Preflight.

```bash
cp -n .env.test.example .env.test
set -a
. ./.env.test
set +a

export SLEEPAGENT_PG16_BIN=/mnt/data4/wz/.sleepagent/pg16/bin
export SLEEPAGENT_PG16_VERIFY_ROOT=/tmp/sleepagent-postgres16-verification
install -d -m 700 "$SLEEPAGENT_PG16_VERIFY_ROOT/socket"
printf '%s\n' "$SLEEPAGENT_TEST_POSTGRES_ADMIN_PASSWORD" \
  > "$SLEEPAGENT_PG16_VERIFY_ROOT/admin-password"
chmod 600 "$SLEEPAGENT_PG16_VERIFY_ROOT/admin-password"

"$SLEEPAGENT_PG16_BIN/initdb" \
  -D "$SLEEPAGENT_PG16_VERIFY_ROOT/pgdata" \
  -U "$SLEEPAGENT_TEST_POSTGRES_ADMIN_USER" \
  --auth-local=trust --auth-host=scram-sha-256 \
  --pwfile="$SLEEPAGENT_PG16_VERIFY_ROOT/admin-password" --no-instructions
"$SLEEPAGENT_PG16_BIN/pg_ctl" \
  -D "$SLEEPAGENT_PG16_VERIFY_ROOT/pgdata" \
  -l "$SLEEPAGENT_PG16_VERIFY_ROOT/postgres.log" \
  -o "-p 15432 -h 127.0.0.1 -k $SLEEPAGENT_PG16_VERIFY_ROOT/socket -c unix_socket_permissions=0700" \
  -w start
PGPASSWORD="$SLEEPAGENT_TEST_POSTGRES_ADMIN_PASSWORD" \
  "$SLEEPAGENT_PG16_BIN/createdb" -h 127.0.0.1 -p 15432 \
  -U "$SLEEPAGENT_TEST_POSTGRES_ADMIN_USER" \
  "$SLEEPAGENT_TEST_POSTGRES_DB"
```

Readiness and exact version:

```bash
"$SLEEPAGENT_PG16_BIN/pg_isready" -h 127.0.0.1 -p 15432 \
  -d "$SLEEPAGENT_TEST_POSTGRES_DB"
PGPASSWORD="$SLEEPAGENT_TEST_POSTGRES_ADMIN_PASSWORD" \
  "$SLEEPAGENT_PG16_BIN/psql" "$SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN" \
  -Atc "SELECT version(), current_setting('listen_addresses')"
```

Apply migrations, bootstrap only the test roles, verify, and test:

```bash
export SLEEPAGENT_BACKEND_DATABASE_DSN="$SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN"
export SLEEPAGENT_BACKEND_DEPLOYMENT_MODE=test
export SLEEPAGENT_BACKEND_SIGNING_KEY_REF="file:$PWD/tests/fixtures/keys/replay_actor_public.pem"
export SLEEPAGENT_BOOTSTRAP_API_DATABASE_ROLE="$SLEEPAGENT_TEST_POSTGRES_API_USER"
export SLEEPAGENT_BOOTSTRAP_API_DATABASE_PASSWORD="$SLEEPAGENT_TEST_POSTGRES_API_PASSWORD"
export SLEEPAGENT_BOOTSTRAP_DEMO_DATABASE_ROLE="$SLEEPAGENT_TEST_POSTGRES_DEMO_USER"
export SLEEPAGENT_BOOTSTRAP_DEMO_DATABASE_PASSWORD="$SLEEPAGENT_TEST_POSTGRES_DEMO_PASSWORD"
export SLEEPAGENT_BOOTSTRAP_WORKER_DATABASE_ROLE="$SLEEPAGENT_TEST_POSTGRES_WORKER_USER"
export SLEEPAGENT_BOOTSTRAP_WORKER_DATABASE_PASSWORD="$SLEEPAGENT_TEST_POSTGRES_WORKER_PASSWORD"
export SLEEPAGENT_BOOTSTRAP_API_SERVICE_PRINCIPAL_ID="$SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"
export SLEEPAGENT_BOOTSTRAP_DEMO_SERVICE_PRINCIPAL_ID="$SLEEPAGENT_TEST_POSTGRES_DEMO_PRINCIPAL"
export SLEEPAGENT_BOOTSTRAP_WORKER_SERVICE_PRINCIPAL_ID="$SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL"
export SLEEPAGENT_BOOTSTRAP_ACTOR_VERIFICATION_KEY_SHA256="$SLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_SHA256"

python -m sleepagent.persistence.migrate apply
python -m sleepagent.persistence.test_bootstrap
python -m sleepagent.persistence.migrate check
python -m pytest -q -m postgres
```

To reset, first confirm that the DSN names the test database. The next two
commands delete and recreate only `sleepagent_replay_test`; then repeat the
apply/bootstrap/check/test block above.

```bash
test "$SLEEPAGENT_TEST_POSTGRES_DB" = sleepagent_replay_test
PGPASSWORD="$SLEEPAGENT_TEST_POSTGRES_ADMIN_PASSWORD" \
  "$SLEEPAGENT_PG16_BIN/dropdb" --if-exists -h 127.0.0.1 -p 15432 \
  -U "$SLEEPAGENT_TEST_POSTGRES_ADMIN_USER" sleepagent_replay_test
PGPASSWORD="$SLEEPAGENT_TEST_POSTGRES_ADMIN_PASSWORD" \
  "$SLEEPAGENT_PG16_BIN/createdb" -h 127.0.0.1 -p 15432 \
  -U "$SLEEPAGENT_TEST_POSTGRES_ADMIN_USER" sleepagent_replay_test
```

Stop the isolated cluster:

```bash
"$SLEEPAGENT_PG16_BIN/pg_ctl" \
  -D "$SLEEPAGENT_PG16_VERIFY_ROOT/pgdata" -w stop -m fast
```

Optional teardown after it is stopped removes only the exact documented temp
directory:

```bash
test "$SLEEPAGENT_PG16_VERIFY_ROOT" = /tmp/sleepagent-postgres16-verification
rm -rf -- /tmp/sleepagent-postgres16-verification
```

The PostgreSQL marker currently includes one evidence-reader test that skips
unless a completed process-proof root operation ID is supplied. It is not a
database readiness failure; the other 32 PostgreSQL tests form the runnable
fresh-database baseline.
