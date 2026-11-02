# Study Habit Engine

A private Telegram study planner for one person. It helps you turn learning topics into a realistic daily queue, record the time you actually study, and keep unfinished work from becoming catch-up debt.

> **Before you start:** This project does not provide a hosted bot. You run your own copy and connect your own Telegram bot. The setup is intended for local development and personal use; production security and recovery have not been established.

## What it helps you do

- **Plan today's study:** Get a queue based on your available time and topics whose prerequisites are complete.
- **Keep missed days manageable:** Unfinished topics return to the unscheduled backlog; the planner does not pile up missed days as overdue work.
- **Track real progress:** Record study minutes, review past sessions, and explicitly mark a topic complete when you have learned it. Finishing a study block alone does not mark its topic complete.
- **Build a curriculum:** Import your own reviewed curriculum, or use `/learn` to draft a proposal for programming and computer-science topics. You must review and approve a proposal before it becomes active.

## Use the Telegram bot

Start with `/help` in your private chat. These are the main commands:

| Command | In plain language |
| --- | --- |
| `/today` | Show today's study queue. |
| `/budget [minutes]` | Set how much time you have today (up to 90 minutes). |
| `/replan` | Rebuild today's unfinished queue. |
| `/done` | Record how many minutes you studied for a block. |
| `/skip` | Skip a block without recording study time. |
| `/complete_topic` | Mark a topic complete or reopen it after reviewing the action. |
| `/progress` | Review study history or add/correct a past session. |
| `/learn` | Draft a programming/CS learning plan; reply with your goal and weekly study time. |
| `/approve <proposal_id>` | Approve a reviewed proposal and make it the active curriculum. |
| `/reject <proposal_id>` | Reject a proposal without changing the active curriculum. |

### A few important rules

- The daily budget defaults to 60 minutes and cannot exceed 90 minutes. Study blocks are 15–30 minutes; actual study time is recorded separately from the planned block length.
- Completing a block records a study session. It does **not** prove that you have completed the topic.
- `/learn` currently supports programming and computer science because it searches a small trusted-source catalog. A request with no trusted sources is declined without changing your curriculum.
- Approving a learning-plan proposal replaces the active curriculum. Historical minutes stay with their original topic IDs; topic-completion status does not carry over. Review the proposal warning before approving.
- AI may help draft learning plans or optionally reorder a daily queue, but it does not override prerequisite checks or time limits. A generated plan is a suggestion, not a guarantee of teaching quality.

## Run your own bot

The repository includes no ready-made curriculum and no public bot instance. To configure Telegram, start the application, and optionally add AI or Calendar features, follow the beginner-oriented [self-hosting guide](docs/SELF_HOSTING.md).

## For contributors

To run the complete local test suite, see **Testing** in the [self-hosting guide](docs/SELF_HOSTING.md). The app uses a Rust Telegram bot, Python API and planning worker, and PostgreSQL.

For the learning-plan quality checks and trusted-source policy, see the [quality-review rubric](docs/design/learning-plan-quality-rubric.md) and [source-policy research](docs/research/ai-learning-plan-rubric-and-source-policy.md).

## Help and project status

- Questions or bugs: [open a GitHub issue](https://github.com/Jonathan0823/study-habit-engine/issues).
- This is a single-user project, not a multi-user service.
- The local Compose setup is not production deployment guidance. Do not expose its API or database to an untrusted network.
