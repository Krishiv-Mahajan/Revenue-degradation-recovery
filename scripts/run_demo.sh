#!/usr/bin/env bash
# One-command Revenue Guardian demonstration on the disposable test database only.
#
#   ./scripts/run_demo.sh
#
# Starts the db_test container (localhost:5434, tmpfs: its data disappears when it stops), migrates it
# to the Alembic head, then runs scripts/demo_end_to_end.py --reset, which refuses any other database.
# The development database (db, port 5433) is never started or touched.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python )}"
TEST_URL="postgresql+asyncpg://postgres:password@localhost:5434/payment_recovery_test"

docker compose --profile test up -d db_test
for _ in $(seq 1 60); do
  if docker compose --profile test exec -T db_test pg_isready -U postgres -d payment_recovery_test >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

DATABASE_URL="$TEST_URL" ALEMBIC_TARGET_DATABASE=payment_recovery_test "$PYTHON" -m alembic upgrade head
DATABASE_URL="$TEST_URL" "$PYTHON" -m scripts.demo_end_to_end --reset
