# Study Habit Engine

A personal Telegram study planner that turns your chosen learning material into a prerequisite-aware daily study queue—without accumulating catch-up work when you miss a day.

Record the time you actually study, mark topics complete when you have finished learning them, and plan from the work that is eligible now. Study blocks are not fixed-time appointments, and a missed day does not create overdue work.

> **Project status:** This repository contains a local implementation, but it is not production-ready. Planning is deterministic by default. Optional Gemini ranking can reorder eligible topics when explicitly enabled; deterministic validation, allocation, and fallback remain authoritative. Operational requirements such as backup and recovery have not been verified in production.

## What you can do

### Choose what to learn

- Choose a subject and review a curriculum of topics, prerequisites, objectives, and learning resources. Provide one yourself or use `/learn` to generate an AI-drafted proposal for programming and computer science; review and approve it before activation. The app does not certify a plan's teaching quality.
- Preview a curriculum's effects on completed topics and existing queues before activating it.
- When changing topic identities, map completed topics explicitly so that progress is not silently reassigned.

### Get a plan for today

- Get a queue sized to your available time and limited to topics whose prerequisites are complete. Topics with recorded study time are prioritized.
- Change today's budget (default 60 minutes; maximum 90) or request a replacement queue.
- Miss a day without accumulating catch-up work. Each queue belongs to one local study date; unfinished work returns to the unscheduled backlog.

### Track your progress

- Record the time you actually studied or skip a block. Completing a block does not automatically complete its topic.
- Review study history, add or correct a past session, and explicitly complete or reopen topics. Changes are confirmed before they are applied.

### What AI does today—and what is planned

Optional Gemini can reorder eligible topics in a daily queue. Deterministic rules still enforce prerequisites, time limits, and allocation. The goal-based learning-plan flow from [issue #28](https://github.com/Jonathan0823/study-habit-engine/issues/28) is now available in Telegram for programming and computer-science goals, using a small trusted-source catalog. Use `/learn`, describe your goal and weekly study time, review the sourced proposal and clearly labeled AI self-review notes, then `/approve <proposal_id>` to activate it or `/reject <proposal_id>` to leave your active plan unchanged. Goals with no sources in the current catalog are declined without changing the active curriculum. The self-review is guidance, not proof of effectiveness; see the [quality-review rubric](docs/design/learning-plan-quality-rubric.md). Ongoing AI tutoring is a separate idea.

## How planning works

The planner creates a queue for the current day from eligible topics and the remaining time budget. By default it prioritizes topics with recorded study time, then follows curriculum order. If explicitly enabled, Gemini can reorder eligible topics, but deterministic code still enforces prerequisite eligibility, started-topic priority, block duration, and capacity. Blocks are up to 30 minutes; the planner does not create a block shorter than 15 minutes. Planning time limits do not restrict truthful progress records: actual study time is recorded separately.

Topics and study blocks are different things. A study block records a bounded session; completing it does not prove that a topic is complete. Topic completion is an explicit action that affects prerequisite eligibility.

The worker checks for the current day's routine plan at 17:00 Asia/Jakarta. It reuses a valid queue, avoids a duplicate Telegram routine message after `/today`, skips zero-minute days, and does not replay missed dates after downtime. If no Calendar authorization is configured, the Telegram routine remains independent.

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
| `/learn` | Start a guided programming/computer-science learning-plan proposal; reply with your goal, then weekly study minutes. |
| `/approve <proposal_id>` | Approve and activate the reviewed learning plan. |
| `/reject <proposal_id>` | Reject the proposal without changing your active learning plan. |

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

The planning worker bind-mounts `GOOGLE_CALENDAR_SECRET_DIR` (default `./secrets`) read-only. Create the host directory before starting Compose, even if Calendar is not configured yet:

```sh
mkdir -p ./secrets
# If GOOGLE_CALENDAR_SECRET_DIR is customized, create that path instead:
mkdir -p /path/to/calendar-secrets
```

The custom path must match `GOOGLE_CALENDAR_SECRET_DIR` in `.env`; Compose mounts that host directory at the worker's `/run/secrets`.

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
| `GEMINI_API_KEY` | Optional key for Gemini topic ranking and learning-plan drafts. |
| `GEMINI_MODEL` | Pinned free-tier model ID; defaults to `gemini-3.8-flash`. Other unpriced model IDs are refused. |
| `GEMINI_FREE_TIER_ACKNOWLEDGED` | Must be `true` to enable Gemini. Only set it after verifying the Google project is on the free tier. |
| `AI_MAX_INPUT_TOKENS` / `AI_MAX_OUTPUT_TOKENS` | Topic-ranking request limits; defaults are 8192 and 512. |
| `TAVILY_API_KEY` | Optional Tavily key for searching only the reviewed programming/CS source domains. |
| `TAVILY_MAX_MONTHLY_CREDITS` | Local monthly search cap, at most 1,000 requests; defaults to 1,000. |
| `AI_PLAN_MAX_INPUT_BYTES` / `AI_PLAN_MAX_OUTPUT_TOKENS` | Learning-plan prompt/output ceilings; defaults are 32,768 bytes and 4,096 tokens. |
| `GOOGLE_CALENDAR_SECRET_DIR` | Optional host directory containing the Calendar OAuth refresh-token file; defaults to the Git-ignored `./secrets`. |

Gemini is optional and disabled by default. Set `GEMINI_FREE_TIER_ACKNOWLEDGED=true` only after confirming the Google project is on the free tier. Topic ranking falls back deterministically on errors; learning-plan generation instead fails safely and leaves the current curriculum untouched. Plan proposals also require `TAVILY_API_KEY`; the worker reserves a local search credit before each request and stops at the configured monthly cap (never above 1,000). Provider billing is controlled by your Google/Tavily account settings, so use free-tier projects/accounts if you require no paid usage. Do not use these integrations for sensitive learner data.

Compose reads `.env`, but commands run directly with `uv` do not. Before using the curriculum client, set `ADMIN_API_URL` and export `ADMIN_SERVICE_TOKEN` in your shell.

### Optional Google Calendar reminders

The routine plan is delivered in Telegram at 17:00 Asia/Jakarta. If configured, the planning worker also creates one private reminder on your primary Google Calendar. It contains only topic titles and planned minutes; it does not include learning-resource links or study history. `/today` does not suppress the Calendar reminder. Without Calendar credentials, planning and Telegram continue unchanged.

To connect a personal account:

1. In Google Cloud, enable the Google Calendar API and configure the OAuth consent screen. Create an OAuth client of type **Desktop app** and download its client-secret JSON to a private location outside the repository.
2. With the local Python environment installed, run the one-time browser flow:

   ```sh
   uv run python scripts/google_calendar_auth.py --client-secret-file /path/to/oauth-client.json
   ```

   The script opens Google authorization, requests only Calendar event access, and writes the refresh credentials to `secrets/google-calendar.json` with mode `0600`. That directory is Git-ignored and mounted read-only into the planning worker; the token is never stored in PostgreSQL. Use `--force` to replace the token after revocation or reconnecting.
3. Set `GOOGLE_CALENDAR_SECRET_DIR` in `.env` only if you chose a different host directory, then start or leave the Compose stack running. The worker discovers the credential file without a restart.

Only the event ID generated by this app is fetched, updated, or removed; the worker never lists or modifies other Calendar events. To disable Calendar, remove the credential file. Existing app events are not automatically deleted when the optional integration is disabled.

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
- **Python FastAPI service:** authenticates bot requests and curriculum-management requests and handles study operations.
- **Python planning worker:** processes durable jobs, optionally requests a bounded Gemini ranking, uses deterministic validation, allocation, and fallback, and synchronizes optional Google Calendar reminders.
- **PostgreSQL:** stores curriculum, progress, plans, jobs, operations, and pending deliveries.
- **LangGraph:** provides the worker's graph/checkpoint structure; the optional Gemini ranking call runs within the planning path.

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
- `/learn` is initially limited to programming and computer science because its exact-host trusted-source catalog is intentionally small. Goals without trusted sources are declined without changing the active curriculum. The app does not certify a plan's teaching quality or treat AI self-review notes as proof.
- Optional Gemini ranking is disabled by default and only reorders topics eligible for today's queue. `/learn` separately drafts a curriculum proposal; neither feature allocates study blocks, which remain deterministic.
- Production readiness, backup and restore, external monitoring, and operational recovery have not been established or verified.
- Telegram retains incoming updates for a limited time. A prolonged outage can mean an update was never accepted; recovery of every command is not guaranteed.

For the approved design context and decision history, see the [parent technical-design issue](https://github.com/Jonathan0823/study-habit-engine/issues/1).
