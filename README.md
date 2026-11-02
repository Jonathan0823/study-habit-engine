# Study Habit Engine

A single-user Telegram study planner that turns an approved curriculum into a prerequisite-aware daily study queue—without accumulating catch-up debt when you miss a day.

Record the time you actually study, mark topics complete when you have finished learning them, and plan from the work that is eligible now. Study blocks are not fixed-time appointments, and a missed day does not create overdue work.

> **Project status:** This repository contains a local implementation, but it is not production-ready. Planning is deterministic and model-free today. AI-assisted ranking is not implemented, and operational requirements such as backup and recovery have not been verified in production.

## What you can do

### As a learner

- Get a daily queue built from your available budget and topics whose prerequisites are complete. Topics with recorded study time are prioritized before other eligible topics.
- Change today's planning budget (default 60 minutes; maximum 90) or request a replacement queue.
- Record the actual time spent on a block or skip it. Completing a block does not automatically complete its topic.
- Review study history, add or correct a past session, and explicitly complete or reopen topics. These changes are confirmed before they are applied.
- Miss a day without building up catch-up work. The queue is for that local study date; unfinished work returns to the unscheduled backlog.

### As an operator

- Import a manually reviewed curriculum containing stable topic IDs, prerequisites, objectives, and learning resources.
- Validate a curriculum and preview its effects on topic completion and existing queues before activation.
- Use explicit completion mappings when revising curriculum identities; historical study time is not silently reassigned.
- Administer curriculum through the authenticated API client rather than writing directly to PostgreSQL.

### Under the hood

- PostgreSQL stores curriculum, progress, plans, operations, planning jobs, and pending deliveries.
- Telegram updates are admitted durably before the bot advances its polling offset, and duplicate updates do not repeat the underlying operation.
- Outgoing messages use a database-backed delivery queue. Delivery is at-least-once: a lost send receipt can result in a duplicate message.
- Planning currently uses deterministic rules. No AI service or model credentials are needed.

## How planning works

The planner creates a queue for the current day from eligible topics and the remaining time budget. It prioritizes topics with recorded study time, then follows curriculum order. Blocks are up to 30 minutes; the planner does not create a block shorter than 15 minutes. Planning time limits do not restrict truthful progress records: actual study time is recorded separately.

Topics and study blocks are different things. A study block records a bounded session; completing it does not prove that a topic is complete. Topic completion is an explicit action that affects prerequisite eligibility.

## Use the Telegram bot

| Command | What it does |
| --- | --- |
| `/help` | Show the supported commands and their usage. |
| `/today` | View today's queue, creating one if needed. |
| `/done` | Select a pending block, then confirm how many minutes you actually studied. |
| `/skip` | Select a pending block to skip; it records no time and does not refill the queue. |
| `/complete_topic` | Complete an eligible topic or reopen a completed topic after reviewing the action. |
| `/budget [minutes]` | Set today's planning budget to 0–90 minutes. With no argument, the bot prompts you. |
| `/replan` | Request a replacement for today's unfinished queue. |
| `/progress [date or action]` | Review history, add or correct a session, or reopen a topic. Dates use `YYYY-MM-DD` in `Asia/Jakarta`. |

The bot shows available targets when needed. Confirmations expire and can become stale if study state changes. For `/progress`, supported actions include `add`, `edit`, `reopen`, and `confirm`; see `/help` for the usage text.

## Run locally

### Requirements

- Docker Compose or Podman Compose with a working container engine.
- A Telegram bot token and the numeric Telegram IDs for the bot, allowed user, and private chat.
- `uv` and Rust/Cargo to run the end-to-end checks.

### Configure and start

Copy the example environment file and replace its placeholders with private values:

```sh
cp .env.example .env
```

Keep `.env` private; it is ignored by Git. Do not put real credentials in `.env.example` or commit them.

Start the stack:

```sh
docker compose up --build
# or: podman compose up --build
```

Compose reads the repository-root `.env` file. Values exported in your shell take precedence over values in `.env`. The stack starts PostgreSQL, applies the schema migration, then runs the API, planning worker, and Telegram bot.

The API is bound to `127.0.0.1:8000`. PostgreSQL is bound to `127.0.0.1:5432` by default; set `DB_HOST_PORT` in `.env` or your shell to change its host port. Once the bot is running, open its private Telegram chat and send `/help`.

Stop the stack with `docker compose down` or `podman compose down`. Add `-v` only if you intend to delete the local PostgreSQL data volume.

> **Local development only:** The Compose database credentials are local-only. Do not expose the API or database to an untrusted network or treat this setup as production deployment guidance.

### Configuration

See `.env.example` for the Compose variables:

| Variable | Purpose |
| --- | --- |
| `BOT_SERVICE_TOKEN` | Bot-to-API authentication credential. |
| `ADMIN_SERVICE_TOKEN` | Curriculum administration API credential. |
| `TELEGRAM_BOT_ID` | Numeric Telegram bot ID. |
| `TELEGRAM_USER_ID` | Numeric ID of the one authorized user. |
| `TELEGRAM_PRIVATE_CHAT_ID` | Numeric private chat ID. |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token; keep it secret. |
| `DB_HOST_PORT` | Optional host port for PostgreSQL; defaults to `5432`. |

Compose reads `.env`, but commands run directly with `uv` do not. Before using the curriculum client, set `ADMIN_API_URL` and export `ADMIN_SERVICE_TOKEN` in your shell.

### Import a curriculum

The repository does not include a ready-to-use curriculum. Create a reviewed JSON file with at least one topic. Each topic needs a stable ID, title, objectives, positive effort estimate, unique position, prerequisite IDs, and at least one reviewed HTTP(S) resource. For example:

```json
{
  "topics": [
    {
      "topic_id": "arrays",
      "title": "Arrays",
      "description": "Understand array indexing and common operations",
      "estimate_minutes": 120,
      "position": 0,
      "prerequisites": [],
      "resources": [
        { "url": "https://example.org/arrays", "title": "Arrays reference" }
      ]
    }
  ]
}
```

Install the pinned Python environment and configure the local admin API URL:

```sh
uv sync
export ADMIN_API_URL='http://127.0.0.1:8000'
export ADMIN_SERVICE_TOKEN='<the admin token from .env>'
```

Import, validate, preview, then activate the curriculum. Use the version and confirmation token returned by the preceding commands:

```sh
uv run python scripts/curriculum.py curriculum import curriculum.json
uv run python scripts/curriculum.py curriculum validate VERSION
uv run python scripts/curriculum.py curriculum preview VERSION
uv run python scripts/curriculum.py curriculum activate CONFIRMATION_TOKEN
```

Review the preview's content hash and impact before activation. For revisions that require explicit completion mappings, pass a mapping file with `preview VERSION --mapping mapping.json`. The client uses the authenticated API; it does not write directly to PostgreSQL.

## Verify

Run the end-to-end checks:

```sh
uv sync
./scripts/test-e2e.sh
```

The script starts a disposable PostgreSQL/API stack, runs Python and Rust tests, and exercises the bot process. It requires `uv`, Cargo, a reachable Docker or Podman engine with Compose support, and network access for initial dependency and image downloads. The script removes its disposable database volume when it exits.

## Architecture

- **Rust Telegram bot:** polls Telegram, submits updates to the API, and sends queued responses.
- **Python FastAPI service:** authenticates the bot and curriculum operator and handles study and curriculum operations.
- **Python planning worker:** processes durable jobs and builds deterministic queues.
- **PostgreSQL:** stores curriculum, progress, plans, jobs, operations, and pending deliveries.
- **LangGraph:** provides the worker's graph/checkpoint structure; the current planning path does not call an AI model.

## Repository map

```text
app/                  Python API, curriculum, commands, and planning worker
bot/                  Rust Telegram bot
db/                   PostgreSQL initialization
scripts/              Curriculum client and end-to-end test helpers
tests/                Python tests
bot/tests/            Rust integration tests
docs/agents/          Contributor and agent workflow guidance
```

## Scope and limitations

- The first version is for one authorized Telegram user, not a multi-user service.
- Curriculum content and resources are manually reviewed; the engine does not generate or independently verify them.
- AI-assisted ranking is not implemented.
- Production readiness, backup and restore, external monitoring, and operational recovery have not been established or verified.
- Telegram retains incoming updates for a limited time. A prolonged outage can mean an update was never accepted; recovery of every command is not guaranteed.

For the approved design context and decision history, see the [parent technical-design issue](https://github.com/Jonathan0823/study-habit-engine/issues/1).
