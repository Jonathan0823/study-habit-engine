#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v podman >/dev/null 2>&1; then
  compose=(podman compose)
elif command -v docker >/dev/null 2>&1; then
  compose=(docker compose)
else
  echo "Podman or Docker Compose is required" >&2
  exit 1
fi
trap '"${compose[@]}" down -v' EXIT
DB_HOST_PORT=$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
export DB_HOST_PORT
uv sync --group test
export TELEGRAM_BOT_TOKEN=test-token
export DATABASE_URL="postgresql://study_runtime:study-runtime-local@127.0.0.1:${DB_HOST_PORT}/study"
export MIGRATION_DATABASE_URL="postgresql://postgres:postgres-local-only@127.0.0.1:${DB_HOST_PORT}/study"
export BOT_SERVICE_TOKEN=bot-secret
export ADMIN_SERVICE_TOKEN=admin-secret
export TELEGRAM_BOT_ID=12345
export TELEGRAM_USER_ID=111
export TELEGRAM_PRIVATE_CHAT_ID=111
export TEST_API_URL=http://127.0.0.1:8000
"${compose[@]}" up -d --build api
ready=0
for _ in $(seq 1 60); do
  if curl -fsS "$TEST_API_URL/health/ready" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  "${compose[@]}" logs api db migrate
  exit 1
fi
cargo test --test http_path
uv run python -m pytest -q
uv run python scripts/test_bot_process.py
