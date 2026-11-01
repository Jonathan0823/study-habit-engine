# Study Habit Engine — ticket 10 slice

A minimal Rust Telegram transport and Python admission API. Python stores update dispositions and `/help` delivery intents in PostgreSQL in one transaction; the bot confirms a Telegram polling offset only after the API commits. Delivery is at-least-once: a lost send receipt can result in a duplicate Telegram message, never a repeated operation.

## Run locally

Install the pinned Python environment with `uv sync`. Configure the numeric Telegram IDs and service secret, then start the separately supervised API and bot containers with Podman Compose:

```sh
export BOT_SERVICE_TOKEN=<private-bot-service-secret>
export ADMIN_SERVICE_TOKEN=<private-admin-service-secret>
export TELEGRAM_BOT_ID=<numeric-bot-id>
export TELEGRAM_USER_ID=<numeric-allowed-user-id>
export TELEGRAM_PRIVATE_CHAT_ID=<numeric-private-chat-id>
export TELEGRAM_BOT_TOKEN=<telegram-token>
podman compose up --build
```

Compose starts PostgreSQL from `docker.io/library/postgres:17-alpine`, runs the schema migration with the migration DB role, then starts the API with a separate runtime DB role and the Telegram bot. The API port is loopback-bound for local development. Never expose it outside a private network or use the local-only PostgreSQL credentials in production.

## Verify

`./scripts/test-e2e.sh` uses `uv` for Python dependency and test management, starts a disposable PostgreSQL/API stack, tests persistence and rejection behavior, and runs a Rust simulated-update -> API -> outbox -> delivery-result integration test. It requires a reachable Docker or Podman engine with Compose support, `uv`, Cargo, and network access for initial dependency/image downloads. It prefers a reachable Docker engine, then falls back to a usable Podman Compose runtime.

The operator curriculum client uses the authenticated admin API (no direct database access): set `ADMIN_API_URL` and `ADMIN_SERVICE_TOKEN`, then run `uv run python scripts/curriculum.py curriculum import curriculum.json`, `validate VERSION`, `preview VERSION`, and `activate CONFIRMATION_TOKEN`. Review the hash and impact shown by preview before activation.
