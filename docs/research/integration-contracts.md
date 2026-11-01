# Integration contracts: LangGraph, Telegram, and Rust polling

Research for [Verify LangGraph checkpoint and Telegram ingestion contracts](https://github.com/Jonathan0823/study-habit-engine/issues/9). Primary-source documentation research only; no runtime verification was performed.

## LangGraph PostgreSQL async checkpointer

- LangGraph documents PostgreSQL as a separately-installed checkpointer integration, package `langgraph-checkpoint-postgres`, exposing `AsyncPostgresSaver`; async graph execution uses async saver methods. A checkpointer is supplied when compiling the graph. [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- The documented identity is `configurable.thread_id`; it is the key used to save/load checkpoints. Threads accumulate state across graph runs. Thus to keep each accepted Telegram event/job independent, issue #9's one-attempt/job design should provide a unique stable job identity as the thread id and avoid reusing it for unrelated jobs. This last mapping is a design inference, not a LangGraph requirement. [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- Official setup docs give `pip install -U "psycopg[binary,pool]" langgraph langgraph-checkpoint-postgres`, import `AsyncPostgresSaver` from `langgraph.checkpoint.postgres.aio`, and show `async with AsyncPostgresSaver.from_conn_string(DB_URI) as checkpointer:`. The docs explicitly require `await checkpointer.setup()` the first time; setup creates tables/runs migrations. [LangGraph Memory: Postgres checkpointer](https://docs.langchain.com/oss/python/langgraph/add-memory)
- The official package source at `main` confirms `from_conn_string` is an async context manager; it opens `AsyncConnection.connect(... autocommit=True, prepare_threshold=0, row_factory=dict_row)` and yields a saver. `setup()` executes migrations. This is implementation-source evidence, not a compatibility guarantee for an unspecified installed version. [AsyncPostgresSaver source](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py)
- This supported connection constructor owns a single AsyncConnection, not a pool. Source says AsyncConnectionPool is accepted by the constructor, and rejects combining it with a pipeline, but this fetch does not establish an officially recommended application pool lifespan/compatibility recipe. Avoid guessing; select/package-pin and inspect the matching source before opting into a custom pool. The setup/migration call needs database permissions to create/update its schema objects; required exact grants/least-privilege role are not documented in the sources fetched and remain a deployment detail.
- Checkpoint durability modes differ. `sync` writes every checkpoint before the next step; `async` has a stated small crash-loss risk; `exit` doesn't persist intermediate state. LangGraph's docs explicitly say a crash before the first durable checkpoint leaves the thread indistinguishable from never-invoked, and advise recording admission outside the checkpointer (example: committing an outbox row before `invoke`). This directly supports keeping the business acceptance/plan/outbox transaction outside checkpoint authority. [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- Replay from an earlier checkpoint re-executes subsequent nodes, including LLM calls and API requests; it is not cached output replay. Therefore a checkpoint is not a business-side-effect transaction or exactly-once guarantee. Keep plan/outbox effects in a separately transactional/idempotent store as the design requires; avoid unguarded external effects in replayable graph nodes. [LangGraph time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel), [checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)

## Telegram contracts

- `getUpdates` returns updates by long polling. The offset is the first update requested; an update is confirmed when a `getUpdates` call uses an offset greater than its update id. The API recommends recalculating offset after each response to avoid duplicates. Negative offsets discard earlier queued updates. Updates are not delivered by `getUpdates` when a webhook is set. [Telegram getUpdates](https://core.telegram.org/bots/api/methods/getupdates.md)
- Thus application-level durable acceptance must precede sending the next offset that confirms those updates. Persist offset/acceptance state (or re-deliver safely on duplicate) across restart. Advancing offset immediately upon receipt is unsafe for the stated acceptance contract. This ordering is an inference from Telegram's acknowledgement semantics plus the design constraint.
- Telegram documentation says pending updates are retained for no longer than 24 hours; loss is possible if the consumer is unavailable longer. [Telegram Bot API](https://core.telegram.org/bots/api)
- Inline-keyboard `callback_data` must be 1–64 bytes, not characters. Use compact opaque identifiers, not full serialized plans. [InlineKeyboardButton](https://core.telegram.org/bots/api/types/inlinekeyboardbutton.md)
- `answerCallbackQuery` acknowledges a callback query to the client/UI, optionally showing a 0–200-character notice; it is distinct from `getUpdates` offset confirmation. The user may see a loading indicator until answered. It can be answered after durable receipt/acceptance; whether early UI acknowledgement is permissible is a product decision, not a Telegram durability rule. [answerCallbackQuery](https://core.telegram.org/bots/api/methods/answercallbackquery.md)

## Minimal Rust/teloxide integration shape

Teloxide `Polling` is not inherently unusable, but its stock stream is unsafe for this strict boundary unless its automatic offset behavior can be controlled or the processing/ack boundary is adapted. Source evidence: after a successful response it sets its internal offset from `updates.last().id.as_offset()` (lines 427–433), and on graceful stop sends `get_updates` with that offset (lines 474–500); its docs explicitly describe that shutdown request as acknowledgement (lines 186–190, 229–238). The source does not wait for application handler/Python acceptance before updating internal offset. A direct `get_updates` loop gives explicit control. [teloxide polling source](https://docs.rs/teloxide/latest/src/teloxide/update_listeners/polling.rs.html), [Polling docs](https://docs.rs/teloxide/latest/teloxide/update_listeners/struct.Polling.html)

A minimal conceptual loop using teloxide's direct request API should instead:

1. Load durable `next_offset` from application storage (initially absent/0 by policy).
2. Call `Bot::get_updates()` with that offset and a positive timeout; process updates in returned order.
3. Send each update to Python with a stable delivery key such as bot id + `update_id`; Python commits idempotent job/admission and plan/outbox transaction, then returns durable acceptance.
4. Only after acceptance, persist `next_offset = update_id + 1`; the *next* `get_updates` call at that offset confirms it. On timeout/error before acceptance, retry without advancing. On duplicate, Python must deduplicate by delivery key.
5. Optionally answer callback query after acceptance, via `answer_callback_query`; this is UI feedback, not a substitute for durable acceptance.

Teloxide's API exposes get-updates payload fields including offset/timeout, and the request trait describes Bot API requests as awaitable; exact direct method names/signatures should be checked against the selected teloxide version before implementation. [GetUpdates payload](https://docs.rs/teloxide/latest/teloxide/payloads/struct.GetUpdates.html), [Requester](https://docs.rs/teloxide/latest/teloxide/requests/trait.Requester.html)

## Sourced facts vs. unresolved/inferred

**Sourced:** thread identity/checkpoint semantics, async saver availability/package name, durability modes, replay behavior, Telegram offset confirmation and callback limits/answer role, teloxide Polling auto-offset behavior.

**Inference/design guidance:** stable job-derived thread id; offset only after Python's durable acceptance; idempotent delivery keys; avoid side effects in replayable nodes; answer callback after acceptance if that timing fits product UX.

**Unverified at runtime:** exact installed LangGraph/teloxide versions, executing the documented PostgreSQL saver initialization/migrations, process crash behavior at each integration boundary, transport retry semantics, and throughput/concurrency/order behavior. Callback UI acknowledgement timing is a product decision. These require version-pinned implementation/tests or owner approval; no runtime claim is made.

## Remaining blockers

- **Actual design blocker:** none established by these sources beyond the already-stated invariant that the durable business acceptance transaction precede Telegram offset confirmation. The source contracts support that ordering.
- **Implementation details requiring version pinning/tests:** exact LangGraph and teloxide versions; psycopg and PostgreSQL compatibility; whether to use the documented single connection or separately configured pool; successful migration under the production database role; crash/retry behavior across Python acceptance and offset persistence; and idempotency/concurrency behavior. No runtime integration tests were conducted.
- **Product decision:** whether/how to answer callback queries before or after durable acceptance (UI responsiveness vs. acknowledgement timing). Telegram's callback answer is separate from getUpdates offset confirmation.

## Sources

- [LangGraph Postgres setup](https://docs.langchain.com/oss/python/langgraph/add-memory) — documented async import, connection lifecycle, and first-use setup.
- [AsyncPostgresSaver source](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py) — connection defaults and migration implementation (moving main branch, not a version pin).
- [teloxide polling source](https://docs.rs/teloxide/latest/src/teloxide/update_listeners/polling.rs.html) — automatic offset advancement and shutdown acknowledgement (moving latest documentation).
- [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) — package, async support, thread identity, durability and external admission record.
- [LangGraph time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel) — replay re-executes downstream nodes/side-effecting calls.
- [Telegram getUpdates](https://core.telegram.org/bots/api/methods/getupdates.md) — long polling offset acknowledgement.
- [Telegram Bot API](https://core.telegram.org/bots/api) — update retention limit.
- [InlineKeyboardButton](https://core.telegram.org/bots/api/types/inlinekeyboardbutton.md) — callback data bytes limit.
- [answerCallbackQuery](https://core.telegram.org/bots/api/methods/answercallbackquery.md) — callback UI acknowledgement.
- [teloxide Polling](https://docs.rs/teloxide/latest/teloxide/update_listeners/struct.Polling.html) — auto offset and shutdown acknowledgement behavior.
- [teloxide GetUpdates](https://docs.rs/teloxide/latest/teloxide/payloads/struct.GetUpdates.html) and [Requester](https://docs.rs/teloxide/latest/teloxide/requests/trait.Requester.html) — Rust API references (Requester fetch encoding was garbled, so no method signatures asserted).
