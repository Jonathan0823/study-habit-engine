#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
engine_is_usable() {
  local engine=$1
  shift
  command -v "$engine" >/dev/null 2>&1 || return 1
  "$engine" info >/dev/null 2>&1 || return 1
  "$@" version >/dev/null 2>&1 || return 1
  env \
    BOT_SERVICE_TOKEN="${BOT_SERVICE_TOKEN:-ci-probe}" \
    ADMIN_SERVICE_TOKEN="${ADMIN_SERVICE_TOKEN:-ci-probe}" \
    TELEGRAM_BOT_ID="${TELEGRAM_BOT_ID:-1}" \
    TELEGRAM_USER_ID="${TELEGRAM_USER_ID:-1}" \
    TELEGRAM_PRIVATE_CHAT_ID="${TELEGRAM_PRIVATE_CHAT_ID:-1}" \
    TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-ci-probe}" \
    "$@" ps >/dev/null 2>&1
}

compose_project="study-habit-e2e-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
if engine_is_usable docker docker compose; then
  compose=(docker compose --project-name "$compose_project")
elif engine_is_usable podman podman compose; then
  compose=(podman compose --project-name "$compose_project")
else
  cat >&2 <<'EOF'
A reachable Docker or Podman engine with Compose support is required.
Docker must pass `docker info` and `docker compose version`; Podman must pass
`podman info` and `podman compose version`. Start/configure an engine and ensure
its Compose provider is installed, then retry scripts/test-e2e.sh.
EOF
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
