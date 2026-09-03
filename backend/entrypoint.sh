#!/bin/sh
# AgentGate container entrypoint.
#
#   1. Apply hand-written Alembic migrations (Section L of the architecture-freeze
#      doc). `alembic upgrade head` is idempotent -- a no-op when the database is
#      already at head. AgentGate runs as a single container, so there is no
#      migration race to coordinate. Retries a few times so a database that is
#      still waking up does not crash-loop the service.
#   2. Optionally seed the SIMULATED merchant / catalogue / agents when
#      SEED_ON_START=true. `app.seed` is idempotent -- it only writes when the
#      database has no merchant -- so this is safe to leave on for a demo deploy
#      and a no-op on every restart after the first.
#   3. exec uvicorn on $PORT. Render / Railway inject $PORT; compose, `docker run`
#      and Fly use the default 8000.
#
# Set RUN_MIGRATIONS_ON_START=false when the platform runs `alembic upgrade head`
# as its own release / pre-deploy step (e.g. Fly's `release_command`), so it does
# not run twice.
set -e

PORT="${PORT:-8000}"

if [ "${RUN_MIGRATIONS_ON_START:-true}" = "true" ]; then
  attempt=0
  until alembic upgrade head; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 10 ]; then
      echo "entrypoint: 'alembic upgrade head' failed after $attempt attempts; giving up" >&2
      exit 1
    fi
    echo "entrypoint: migration attempt $attempt failed; retrying in 3s..." >&2
    sleep 3
  done
  echo "entrypoint: database is at head"
fi

if [ "${SEED_ON_START:-false}" = "true" ]; then
  echo "entrypoint: seeding SIMULATED demo data (no-op if already seeded)"
  python -m app.seed
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
