# Run your own Study Habit Engine

This guide explains how to run a private, single-user copy of Study Habit Engine. You need your own Telegram bot and computer or server. There is no hosted bot to sign up for.

> **Personal-use setup only:** The supplied Compose configuration is for local development. Do not expose its API or database to the public internet. Production backups, monitoring, and recovery have not been verified.

## What you need

- Docker with Compose, or Podman with Compose, and a working container engine.
- A Telegram bot token, plus the numeric ID of the bot, your Telegram account, and the private chat you will use.
- `uv` and Rust/Cargo only if you want to run the full test suite.

## 1. Get the project

```sh
git clone https://github.com/Jonathan0823/study-habit-engine.git
cd study-habit-engine
```

## 2. Add your private settings

Copy the example settings file and create the optional Calendar secret directory:

```sh
cp .env.example .env
mkdir -p ./secrets
```

Open `.env` and replace the placeholders. The bot needs all of these values:

| Setting | What to enter |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | The token for your bot. Keep it secret. |
| `TELEGRAM_BOT_ID` | The bot's numeric Telegram ID; this is not its token. |
| `TELEGRAM_USER_ID` | The numeric ID of the one person allowed to use this bot. |
| `TELEGRAM_PRIVATE_CHAT_ID` | The numeric ID of that person's private chat with the bot. |
| `BOT_SERVICE_TOKEN` | A private password used by the Telegram bot to authenticate with the API. |
| `ADMIN_SERVICE_TOKEN` | A separate private password for curriculum administration. |

Use different random values for the two service tokens. For example, run `openssl rand -hex 32` twice and use each result once. Do not leave the `000000000` or `replace-with-...` placeholders in place.

Keep `.env` and the files in `secrets/` private. Do not commit real tokens, passwords, or OAuth credentials. If you change `GOOGLE_CALENDAR_SECRET_DIR`, create that host directory and set the same path in `.env`.

## 3. Start the application

```sh
docker compose up --build
```

Or use `podman compose up --build`. Compose starts PostgreSQL, applies the database schema, then starts the API, planning worker, and Telegram bot. The first start downloads images and dependencies, so it needs network access.

When the services are ready, open your bot's private Telegram chat and send `/help`. The bot accepts commands only from the user and chat IDs configured in `.env`.

To stop the services, press `Ctrl+C` if they are running in the foreground, then run:

```sh
docker compose down
```

This keeps the local database volume. Add `-v` only if you intend to delete your saved curriculum and progress. To start again, use `docker compose up`.

## 4. Set up a curriculum

The repository does not include a ready-to-use curriculum. Choose one of these options:

- **Use `/learn`:** In Telegram, send `/learn`, describe a programming or computer-science goal, then enter your weekly study minutes. The bot searches a small trusted-source catalog and sends a draft for review. If it cannot find trusted sources, it declines without changing your current curriculum. Approval replaces the current curriculum; historical minutes remain with their old topic IDs, and completion status does not carry over.
- **Import your own:** Create a reviewed JSON curriculum and use the command-line client below. Every schedulable topic should have reviewed learning resources and valid prerequisites.

To import a curriculum, keep the Compose stack running. In a second terminal, install the Python environment and configure the local API connection:

```sh
uv sync
export ADMIN_API_URL='http://127.0.0.1:8000'
export ADMIN_SERVICE_TOKEN='<the admin token from .env>'
```

The `.env` file is read by Compose, but not automatically by commands run directly with `uv`. Replace the placeholder above with the same private admin token you set in `.env`.

Import, validate, preview, and then activate a curriculum. Use the version or confirmation token returned by the previous command:

```sh
uv run python scripts/curriculum.py curriculum import curriculum.json
uv run python scripts/curriculum.py curriculum validate VERSION
uv run python scripts/curriculum.py curriculum preview VERSION
uv run python scripts/curriculum.py curriculum activate CONFIRMATION_TOKEN
```

Read the preview—including its content hash and impact—before activation. For a revision that changes topic identities, provide an explicit completion mapping: `preview VERSION --mapping mapping.json`. The client sends requests to the authenticated API; it does not write directly to PostgreSQL.

## Optional features

### AI-assisted learning plans and ranking

AI features are optional. To enable them, set the relevant keys in `.env`:

- `GEMINI_API_KEY` enables Gemini features.
- `GEMINI_FREE_TIER_ACKNOWLEDGED=true` is required before Gemini can run. Set it only after confirming your Google project is on the free tier.
- `TAVILY_API_KEY` enables trusted-source search for `/learn`. Searches are limited to the reviewed programming/CS source domains and capped locally by `TAVILY_MAX_MONTHLY_CREDITS` (at most 1,000 requests per month by default).

The daily queue still applies deterministic prerequisite and time-limit rules. AI ranking is disabled by default and falls back to deterministic ranking on errors. Learning-plan generation fails safely if it cannot produce a valid sourced proposal; it does not activate a curriculum automatically. Provider billing is controlled by your Google and Tavily account settings. Do not send sensitive personal data to these services.

### Google Calendar reminder

The bot can optionally add a private reminder at 17:00 Asia/Jakarta. The event contains topic titles and planned minutes, not resource links or study history. Without Calendar credentials, Telegram planning continues normally.

To connect an account:

1. Enable the Google Calendar API in Google Cloud, configure the OAuth consent screen, and create an OAuth client of type **Desktop app**.
2. Save the downloaded client-secret JSON somewhere private and outside this repository.
3. With `uv sync` complete, run:

   ```sh
   uv run python scripts/google_calendar_auth.py --client-secret-file /path/to/oauth-client.json
   ```

4. Complete the browser authorization. The script saves the refresh token to `secrets/google-calendar.json` with restrictive permissions. The worker reads it from the mounted secrets directory; the token is not stored in PostgreSQL.

Use `--force` to replace credentials after revocation or reconnecting. Only events created by this app are managed. Removing the credential disables Calendar integration but does not automatically remove existing app-created events.

## Configuration reference

`.env.example` lists every setting and its default. These are the main optional settings:

| Setting | Purpose |
| --- | --- |
| `DB_HOST_PORT` | Host port for PostgreSQL; defaults to `5432`. |
| `GEMINI_MODEL` | Gemini model used by optional AI features. Unpriced model IDs are refused. |
| `AI_MAX_INPUT_TOKENS` / `AI_MAX_OUTPUT_TOKENS` | Limits for daily queue ranking requests. |
| `AI_PLAN_MAX_INPUT_BYTES` / `AI_PLAN_MAX_OUTPUT_TOKENS` | Limits for learning-plan requests. |
| `TAVILY_MAX_MONTHLY_CREDITS` | Local monthly cap for trusted-source searches. |
| `GOOGLE_CALENDAR_SECRET_DIR` | Host directory containing the Calendar OAuth refresh-token file; defaults to `./secrets`. |

The API and PostgreSQL host ports bind to `127.0.0.1` by default. Do not change these bindings to expose the services to an untrusted network.

## For developers: run the tests

Install the pinned Python dependencies, then run the end-to-end suite:

```sh
uv sync
./scripts/test-e2e.sh
```

The script starts a disposable PostgreSQL/API stack, runs Python and Rust tests, and exercises the Telegram bot process. It requires `uv`, Cargo, a reachable Docker or Podman engine with Compose support, and network access for initial image and dependency downloads. The disposable database volume is removed when the script exits.

## Troubleshooting

- **The bot does not respond:** Check the Compose logs with `docker compose logs -f`. Confirm that the token and all three numeric Telegram IDs in `.env` are correct.
- **Compose reports a missing secret directory:** Create the directory in `GOOGLE_CALENDAR_SECRET_DIR` (default `./secrets`) before starting the stack.
- **Curriculum import cannot connect:** Confirm the stack is running and `ADMIN_API_URL` is `http://127.0.0.1:8000`; export `ADMIN_SERVICE_TOKEN` in the same terminal where you run `uv`.
- **A proposal is declined:** `/learn` only uses its reviewed source catalog. Try a programming/CS goal that can be supported by those sources, or import a curriculum yourself.
