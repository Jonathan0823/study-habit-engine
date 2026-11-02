# Study engine: implementation handoff contract

Status: approved by the owner for implementation handoff. Issue #18's bounded-ranking slice is implemented on `feature/ticket-18-gemini-ranking` and submitted as [PR #26](https://github.com/Jonathan0823/study-habit-engine/pull/26). This contract remains the design reference; see the implementation update below for as-built details and verified limits. The broader system is not production-ready.

## Implementation update — issue #18 (2026-11-02)

- Optional Gemini ranking is wired into the planning worker and disabled by default. The current provider/model allowlist is `google_gemini` / `gemini-3.8-flash`, with pricing revision `gemini-free-tier-v1`; unknown models fall back without dispatch.
- Only approved, prerequisite-eligible topic candidates and verified progress/prerequisite facts are sent. The output must rank every candidate exactly once and pass strict identity, reason-code, and started-topic-order validation. Deterministic code still owns eligibility, block allocation, and plan commit.
- The application monthly spend limit is **US$0**, and the daily call limit is 10. A call requires both `GEMINI_API_KEY` and `GEMINI_FREE_TIER_ACKNOWLEDGED=true`; Compose defaults to disabled. This is an application policy, not a guarantee about external Google account billing. Use a Google project that cannot incur paid usage for a hard zero-cost requirement.
- Each job has at most one persisted dispatch, a 10-second deadline, no configured provider retries, bounded input/output, and deterministic fallback. A dispatched attempt with uncertain outcome is never retried. Reservation checks and attempt insertion occur transactionally; the provider call runs outside the database transaction.
- The current LangGraph has one `snapshot_allocate_commit` node that orchestrates claim/reservation, the optional provider call, a fresh snapshot, revalidation, deterministic allocation, and atomic commit. This is the as-built orchestration; the logical phases are not separate checkpoint nodes. Its checkpoint state contains only the job ID and committed result; PostgreSQL business rows remain authoritative.
- Verification for PR #26: 136 Python tests and the Rust bot HTTP integration test pass; Compose configuration validation passes. No live Gemini API call or production billing behavior was verified.

## Authority and scope

The [technical-design map](https://github.com/Jonathan0823/study-habit-engine/issues/1) indexes the canonical behavior decisions. This asset supplies their concrete storage/interface contract for [Reconcile schema, interfaces, and acceptance criteria for handoff](https://github.com/Jonathan0823/study-habit-engine/issues/8), not replacement versions of those decisions:

- [Curriculum trust](https://github.com/Jonathan0823/study-habit-engine/issues/2#issuecomment-6554601644).
- [Commands and lifecycle](https://github.com/Jonathan0823/study-habit-engine/issues/3#issuecomment-6554634804).
- [Progress](https://github.com/Jonathan0823/study-habit-engine/issues/4#issuecomment-6554655208).
- [Scheduling](https://github.com/Jonathan0823/study-habit-engine/issues/5#issuecomment-6554675416).
- [Service ownership](https://github.com/Jonathan0823/study-habit-engine/issues/6#issuecomment-6554723849).
- [Operations](https://github.com/Jonathan0823/study-habit-engine/issues/7#issuecomment-6554752969).
- [Integration research](https://github.com/Jonathan0823/study-habit-engine/blob/1bfdc9dbdf6813be0bb14ff56e34532734a44bba/docs/research/integration-contracts.md). Source review, not a runtime guarantee.

Rust handles Telegram transport and rendering. Python FastAPI owns business state and command admission; a separate process from the same Python application runs durable jobs and LangGraph. PostgreSQL holds state, jobs, checkpoints, and outbox. No gateway, Redis, web UI, or autonomous agent layer. Use LangGraph with only the necessary LangChain provider integration; ordinary Python implements scheduling, validation, and business transactions. No RAG or vector database in this scope: query approved curriculum and progress directly. A future cited-material tutor would be a separate feature.

## Additional resolved behavior

- Curriculum administration is an SSH operator CLI importing versioned JSON, validating, previewing impact, and explicitly approving/activating the reviewed content. No Telegram curriculum editor.
- Actual time uses integer minutes, 0–1,440 per session, with effective daily total at most 1,440. The 90-minute limit applies to planning, not truthful recording. Zero-minute /done closes the block without starting or completing its topic.
- Historical dates use Asia/Jakarta and cannot be in the future. Split cross-midnight sessions by date. Preserve original data through audited corrections.
- Split/merged objectives get new topic identities. Historical time remains on original identities, never duplicated or automatically redistributed. Completion carryover is separately reviewed against target prerequisites.
- If remaining pending blocks exceed remaining capacity after a time mutation, retain their history but invalidate the pending queue. Offer /replan; do not silently trim/refill. Explicit /budget still rebuilds automatically.
- A callback success/accepted notice follows durable admission, not merely receipt. Temporary-failure notices never claim an uncertain operation failed permanently; retry uses its original action identity.

## PostgreSQL model

UUID below means an opaque application-generated UUID. Revisions and sequences are nonnegative bigint; all timestamps are timestamptz in UTC. Study dates/month boundaries are derived using Asia/Jakarta, not the host timezone. Telegram identifiers use bigint. Money uses exact numeric USD, never floating point. JSON fields have application-validated schemas, not arbitrary model-supplied payloads.

| Record | Required fields and keys |
| --- | --- |
| `engine_state` | Singleton key; active curriculum UUID nullable; `state_revision`; `recovery_epoch` UUID. Lock this row for business mutations and plan commits. |
| `curriculum_versions` | UUID PK; status draft/approved; content hash; created/approved timestamps; approval operation FK nullable. Approved content is immutable; activation is the engine pointer, not another independently editable status. |
| `topics` | Stable text `topic_id` PK (1–64 ASCII letters/digits/underscore/hyphen); created timestamp. Never hard-delete identities referenced by history. |
| `curriculum_topics` | PK (curriculum UUID, topic ID); title; description/objectives; positive integer estimate minutes; unique nonnegative integer curriculum position within the version; nonempty reviewed resource list of `{url, title}`. |
| `prerequisites` | PK (curriculum UUID, topic ID, prerequisite ID); both endpoints composite-FK the same curriculum version; no self-edge. Validate the entire graph is acyclic before approval/activation. |
| `resource_status` | PK (curriculum UUID, topic ID, approved resource URL); availability unknown/available/reported_unavailable; reporter and timestamp. Operator-reported metadata, not editable curriculum content or proof of resource truth. |
| `topic_progress` | Topic ID PK/FK; completed boolean; completion timestamp and approving curriculum UUID nullable; last operation FK. Retain rows for removed topics. Time totals derive from sessions, not a separately editable counter. |
| `study_sessions` | UUID PK; original curriculum/topic composite FK; study date; actual minutes integer 0–1,440; optional block UUID unique; created/last operation FKs. Corrections update effective fields only together with an append-only audit event. |
| `study_days` | Study date PK; budget integer 0–90 (default 60); requested generation sequence; queue revision; current plan UUID nullable; queue state absent/valid/replan_required; routine-suppressed boolean. Create rows lazily. |
| `plans` | UUID PK; date FK; curriculum UUID FK; generating job UUID unique; generation sequence; state revision used; creation timestamp. Structural contents immutable; retain superseded plans. |
| `study_blocks` | UUID PK; plan UUID FK; position unique within plan; topic ID composite-FK the plan curriculum; planned minutes integer 15–45; status pending/done/skipped/cancelled/expired; last operation FK. Repeated topics are allowed, repeated identities/positions are not. |
| `operations` | UUID PK; source key unique; request hash; kind; date nullable; planning job UUID and requested generation nullable; status accepted/completed/rejected; validated request/result payload; created/finished timestamps. The same key with different content is a conflict, not a new action. |
| `confirmations` | Random opaque token PK; actor/chat IDs; recovery epoch; operation kind and validated proposed payload; state/queue revisions; expiry timestamp; consumed operation UUID nullable. Tokens never contain trusted client-side state. |
| `audit_events` | PK (operation UUID, ordinal); entity kind/ID; before/after values; timestamp. Append-only; cover time corrections, topic complete/reopen, mappings, approvals/activation, and budget changes. |
| `planning_jobs` | UUID PK; date FK; status pending/running/completed/failed/expired; desired sequence; claimed sequence; lease owner UUID nullable, fencing generation, lease expiry; committed operation/plan nullable; available-at timestamp. At most one pending/running job per date via partial unique index. |
| `deliveries` | UUID PK; unique logical key; originating operation FK nullable; kind; date/plan/queue revision nullable; validated template payload; status pending/leased/sent/failed/expired/superseded; attempt count; first-attempt/next-attempt/expiry timestamps; lease owner/fence/expiry; Telegram receipt nullable; bounded error category. |
| `telegram_ingestion` | Bot ID PK; durable next offset nullable; poller owner/fence/lease expiry. Only one active poller. |
| `ai_attempts` | Job UUID PK/FK; study date/month start; pinned provider/model/pricing revision; nonnegative `reserved_usd`; nullable nonnegative `actual_usd`; state dispatched/finished/uncertain; nullable validated ranking and fixed error code; creation/finish timestamps. The current free-tier implementation reserves US$0 before dispatch; the primary key enforces at most one attempt per job. Uncertain dispatches are not retried. |

Add foreign keys for all named references. Circular day/plan/job references are inserted nullable then filled in the same transaction, using deferrable constraints where needed. The application migration owns DDL; runtime roles do not auto-migrate on every request.

Approval validation also checks nonempty titles/objectives, unique IDs/order, valid http/https resource URLs without embedded credentials, reviewed resources, references, and cycles. Resource checking must not introduce arbitrary server-side URL fetching: the minimum implementation displays stored links and fixed warnings for operator-reported unavailability. The CLI `resource status VERSION TOPIC URL available|unavailable|unknown` updates only status metadata for a stored approved URL. Unknown means not checked, never verified reachable. Do not build a crawler or silently replace links.

Database CHECK constraints cover scalar bounds and row-local relationships. Cross-row totals, prerequisite eligibility, approved version/hash, mapping impacts, aggregate daily minutes, plan sums, and current revisions are validated under the engine lock in the authoritative transaction. Denormalized display totals are computed from records, never accepted from clients/model output.

LangGraph checkpoint tables are owned by the selected official checkpointer package/migrations; do not recreate its internal schema as business tables.

## Curriculum JSON and CLI

Input shape:

```json
{
  "topics": [
    {
      "topic_id": "arrays",
      "title": "Arrays",
      "description": "Learning objectives reviewed by the owner",
      "estimate_minutes": 120,
      "position": 0,
      "prerequisites": [],
      "resources": [{"url": "https://example.org/arrays", "title": "Reviewed resource"}]
    }
  ]
}
```

The CLI talks to authenticated Python administrative endpoints on the private network; it does not duplicate validation or write tables directly. Its commands are `curriculum import FILE`, `curriculum validate VERSION`, `curriculum preview VERSION --mapping FILE`, and `curriculum activate CONFIRMATION_TOKEN`. First activation accepts an empty mapping. The preview validates and displays the content hash, retained/new/removed IDs, explicit completion mappings, required reopenings including dependents, and queue invalidation. Activation explicitly approves the exact version and applies the reviewed changes atomically. On any revision/hash mismatch, require a fresh preview.

A mapping file contains explicit `{from_topic_ids: [...], to_topic_id: "...", carry_completion: true|false}` entries; no title-based inference. Only reviewed completion carries, not historical time duplication. Carryover must leave all target completions prerequisite-consistent. Retired topics remain selectable for historical correction, not new planning.

## Telegram commands and confirmations

- `/help`: all eight commands below, their argument forms and target-selection instructions; help and dispatch share one command definition contract.
- `/today`: current valid queue; create only if absent. An existing invalidated queue is explained with /replan guidance, not silently regenerated. Routine suppression/17:00 behavior follows the lifecycle decision.
- `/done [block_id] [actual_minutes]`: no target shows choices; omitted minutes are prefilled from planned duration. Show confirmation before recording and closing the selected block.
- `/skip [block_id]`: no target shows choices; selected current pending block becomes skipped, no time and no refill.
- `/complete_topic [topic_id]`: select topic, preview prerequisites/queue impact, confirm.
- `/budget [minutes]`: omitted value prompts; integer 0–90 sets today's total budget and requests a rebuild.
- `/replan`: request today's unfinished queue replacement.
- `/progress [YYYY-MM-DD]`: browse history (today by default), then select add historical session, correct session/date/minutes, or reopen topic. All changes preview and confirm; do not add a second free-form editing language.

The eight supported commands are /help, /today, /done, /skip, /complete_topic, /budget, /replan, /progress.

Confirmations expire after 10 minutes and are bound to the configured actor/chat, recovery epoch, target identities, date if current-day action, and relevant state/queue revisions. Use random URL-safe ASCII tokens no longer than 48 bytes including action prefix, fitting Telegram's 64-byte callback limit. Consume once atomically with the mutation. A replay returns its original operation result before stale-state checks. A different action on an already-closed block does not log more time.

Historical additions allow known retired topics; recording reality does not require reconstructing historical prerequisite eligibility. Completing a current topic still requires current prerequisites. Validate effective daily totals after removing the old value and adding a correction; reject future dates or totals above 1,440 without changing data.

## Internal API v1

All endpoints are private, authenticated, size-bounded JSON. Use separate bot and administrative credentials; restrict each to its endpoints. Validate actor and private-chat IDs against configuration. Clients cannot request another user. Reject unknown fields and unsupported versions. IDs are strings on the wire, including Telegram bigint IDs, to avoid accidental precision loss. Do not log credentials or raw bodies.

| Endpoint | Contract |
| --- | --- |
| `POST /v1/telegram/claim` | Acquire/renew singleton poller lease; returns fence, expiry, durable next offset. |
| `POST /v1/telegram/updates` | Ingest one update in stream order; requires poller fence, bot/update/actor/chat IDs and normalized command/callback payload. Atomically record accepted/rejected operation and advance durable next offset. |
| `GET /v1/operations/{id}` | Return accepted/completed/rejected and safe result/reason codes. No mutation. |
| `POST /v1/deliveries/claim` | Claim one due delivery, checking date/current revision; returns fence, lease, expiry, render payload, and destination. |
| `POST /v1/deliveries/{id}/result` | With fence, record confirmed receipt, transient/ambiguous failure, or permanent failure. Idempotent; reject stale ownership. |
| `POST /v1/admin/curricula/import` | Import validated JSON as draft; content hash identifies repeated import. |
| `POST /v1/admin/curricula/{id}/validate` | Validate draft without activation. |
| `POST /v1/admin/curricula/{id}/preview` | Validate explicit mapping and produce revision-bound approval/activation preview. |
| `POST /v1/admin/confirmations/{token}` | Atomically approve/activate the preview; idempotency key required. |
| `POST /v1/admin/resource-status` | Validate version/topic/stored URL and record operator-reported availability. Idempotency key required; rendering uses fixed warning codes, not user-authored replacement links. |
| `GET /health/live`, `GET /health/ready` | Private health endpoints; readiness checks DB/schema, not model availability. |

Telegram update example:

```json
{
  "bot_id": "12345",
  "update_id": "67890",
  "actor_id": "111",
  "chat_id": "111",
  "poller_fence": 7,
  "payload": {"kind": "command", "command": "budget", "arguments": ["45"]}
}
```

Admission response: `{operation_id, status, result, callback_notice}`. A new async planning request returns HTTP 202 accepted after commit; a completed/read/duplicate operation returns 200. Business rejection is durably recorded and returned with a stable reason so it does not poison the polling queue. Unsupported/unauthorized updates get a durable disposition without storing full messages or responding to unauthorized users. Advance the Telegram offset only through durably disposed updates; never skip an earlier unaccepted update in a fetched batch.

Use HTTP 400 for malformed envelopes, 401/403 for invalid service credentials/scope, 409 for stale ownership/idempotency-body mismatch/confirmation revision conflict, 422 for validated admin business-input errors, and 503 for unavailable DB/schema. Response errors have `{code, retryable, operation_id?, current_queue_revision?}` with fixed safe messages; no stack traces/model prose. Business reason codes include CURRICULUM_UNAPPROVED, PREREQUISITES_INCOMPLETE, STALE_ACTION, REPLAN_REQUIRED, NO_BLOCK_FITS, REST_DAY, TIME_BOUNDS, CONFIRMATION_EXPIRED, MODEL_FALLBACK, and BUDGET_EXHAUSTED.

Uncertain HTTP outcomes are retried under the original source key. Telegram source key is bot ID plus update ID; confirmation mutation identity is its token independent of callback update ID. Administrative writes use a UUID idempotency header plus request hash. Hash semantic request content, not transport fields such as lease fences or retry headers, so a retried update under renewed ownership remains the same operation. No outbound event broker: worker/business events are committed job/outbox records with these schemas.

## Transactions, ordering, and queue validity

Use PostgreSQL READ COMMITTED with the singleton engine row locked before business state checks/changes. Within that lock, read/lock day, target progress/blocks, jobs, and outbox as needed in a consistent order. Include audit and operation completion in the same transaction. Model/network calls never run inside this transaction.

Increment `state_revision` on curriculum activation, progress/time mutations, and budget changes. Increment the day's queue revision on queue replacement or actionable block/status invalidation. Exact successful operation replay returns the saved result without applying revisions twice.

A plan commit checks current date, approved curriculum ID/hash, state revision, day queue revision, requested generation, and lease fence. Check fresh eligibility, unique blocks/positions, durations, exact sum, and remaining budget. New plans replace pending work only; completed session history remains independent. Sum pending blocks against max(0, budget - today's actual minutes), not the original plan's full historical duration sum.

Topic completion cancels that topic's pending blocks; prerequisite reopening cancels affected ineligible blocks. Preserve unaffected blocks if the queue is otherwise valid, but never refill. If remaining queue duration no longer fits, or curriculum activation changes its grounding, mark pending queue replan_required/nonactionable. Keep cancelled/superseded records for history. Budget mutation explicitly invalidates pending work and enqueues a replacement.

At midnight, yesterday's pending blocks become nonactionable even before cleanup writes statuses expired. Queries/actions always check the date. Late recording uses /progress. A yesterday job expires; it cannot silently become today's job.

## Worker, graph, and model contracts

Run one planning worker initially. Durable jobs permit supervised restart, not an in-memory-only task. Worker claims use a 60-second lease, 10-second heartbeat, monotonically increasing fence, and database time. Lost-lease workers cannot commit. Reclaim expired jobs; cap claim-failure retries at five consecutive failures with exponential backoff (5 seconds to 5 minutes), then mark failed/alert. A new explicit /replan may start a fresh job after inspecting the failure. Database unavailability is not an excuse to acknowledge missing work.

Concurrent current-day triggers coalesce into one pending/running job. Each explicit rebuild increments requested generation; /today with an existing valid queue does not. A running worker captures the sequence; newer requested work prevents an obsolete result from settling that request. Each accepted planning operation points to its job and requested sequence. A successful generation settles all waiting operations up to that generation with the current result; later requests remain pending. Mutation-plus-planning commands such as /budget record the applied mutation in their accepted result so retries never reapply it while waiting for the queue. Use one immediate deterministic rebuild after a revision conflict; a second conflict returns pending with a 1-second delay, retaining the AI-attempt state and latest desired sequence.

The intended logical phases are snapshot -> rank_or_fallback -> allocate -> validate_and_commit -> end. As implemented for issue #18, a single `snapshot_allocate_commit` graph node orchestrates those phases through the worker; provider I/O runs outside database transactions, and the final fresh-snapshot validation/plan/outbox commit is guarded by the current lease fence. Stale or invalid model output falls back to deterministic allocation. Business results are committed before graph completion is relied on; a recovered completed job returns its recorded result.

Use official `langgraph-checkpoint-postgres` and `AsyncPostgresSaver` from `langgraph.checkpoint.postgres.aio`; checkpoint tables are provisioned by the migration process, and the worker uses a dedicated async PostgreSQL connection with the job UUID as thread ID. As implemented for issue #18, the JSON graph state contains only the job ID and committed result; the fresh candidate snapshot and business decisions remain in PostgreSQL/worker execution, not checkpoint state. The richer versioned graph state described by this design remains a possible future refinement, not an as-built guarantee. No secrets, open connections, or arbitrary deserialized application objects are checkpointed; checkpoints are never evidence of unapplied business effects.

Model input is a JSON candidate list containing only approved IDs, titles/objectives, curriculum positions, estimates, positive-time/started information, and prerequisite facts. No Telegram text, credentials, tools, browsing, or retrieved instructions. Treat curriculum prose as data. Output schema:

```json
{"ranked_topics": [{"topic_id": "arrays", "reason_code": "NO_PREREQUISITES"}]}
```

`reason_code` is optional; when present it is one of CONTINUE_STARTED, PREREQUISITES_COMPLETE, NO_PREREQUISITES and must match facts. Reject extra fields, missing/unknown/duplicate IDs, false reasons, or group-order violations; reject the whole ranking, not selected bad entries. All candidates appear exactly once. No model-generated durations/links/prose. A valid ranking is only input to deterministic allocation, never an executable plan.

Persist the single AI attempt before dispatch. Its deadline is 10 seconds; disable provider retries. At recovery, a dispatched/uncertain attempt without a saved ranking falls back and is never redispatched. Transactionally enforce the 10-call daily limit and the US$0 local-month spend ceiling under the engine lock. The current pinned model has zero application reservation; unknown model pricing, invalid/missing limits, or oversized input use deterministic fallback. No planning depends on AI availability.

## Polling, delivery, and restore

Use direct teloxide request APIs for explicit polling offset control rather than assuming stock Polling waits for Python handlers. Maintain one leased poller and submit each fetched update in order. Python commits disposition plus next offset before Rust makes a getUpdates request confirming it. On uncertain admission, retry the same event; after restart load the durable cursor. Do not use negative offsets/drop_pending_updates as routine recovery. Ensure webhook mode is not active before polling.

Answer callback queries promptly after confirmed admission; do not wait for planning completion. If the callback answer itself expires/fails, accepted work is still durable and its outbox result remains authoritative. A temporary-failure UI notice must not assert that an uncertain write did not happen.

Delivery leases use 60 seconds and fencing; a bot must stop dispatching when ownership is lost and must bound a Telegram send within its lease. Fence checks protect database receipts, not exactly-once external sends. Dedupe routine intents by study date; explicit request responses have operation-derived keys. Recheck current queue/date when claiming plan delivery and return only approved render data. Expire plan cards at the local day boundary and other ephemeral response deliveries at their configured lifetime (default one hour); all are additionally bounded by the approved one-hour retry window.

Render only templates and approved facts; escape Telegram markup. API-provided opaque action tokens fit the callback limit. A stale message can still arrive during a race with mutation: its buttons must fail safely. Keep the approved retry/backoff and permanent-failure/email behavior. Replaying receipts cannot commit progress.

Telegram retains incoming updates for no longer than 24 hours: prolonged downtime may lose commands never durably accepted. The bot cannot guarantee their recovery; the owner must review history and re-enter missing sessions through /progress. No-success-without-acceptance remains enforceable.

Off-VM backup/restore, monitoring, and credential rotation follow the operations decision. Restore in maintenance mode; generate a new recovery epoch, invalidate confirmation tokens/leases, compare surviving history/delivery receipts, reconcile uncertain lost operations, and resume only after operator review. Do not blindly replay restored messages or assume revoked credentials are revoked by a database restore.

## Configuration and deployment inputs

Required: configured Telegram user/private-chat/bot IDs; separate bot/admin service credentials; bot token (Rust only); runtime DB and migration credentials; backup destination/key; external monitor/email destination; pinned application/dependency/PostgreSQL versions. Gemini ranking is optional and disabled unless a key and explicit free-tier acknowledgement are configured; unknown models/pricing or invalid limits retain deterministic fallback. Missing security/database configuration prevents readiness.

Fixed initial behavior: Asia/Jakarta, default daily budget 60, cap 90, deterministic target block 30 (15-minute minimum), routine 17:00, AI deadline 10 seconds, **US$0/month application spend limit**, 10 calls/day, maximum input 8,192 tokens, default output 512 tokens (hard maximum 1,024), confirmation TTL 10 minutes, leases 60 seconds/heartbeat 10 seconds, delivery retry up to one hour. Override product constants only through a new explicit decision, not undocumented environment switches. Provider/host/monitoring vendor and secret values are deployment inputs; changing business guarantees is not.

Before launch, pin and test the chosen framework versions, migrations, readiness, roles, secret handling, durable volumes, external alerts, and backup restore. No implementation or provisioning is part of this design map.

## Executable-style acceptance matrix

Each row is a required automated or operational test, not a claim of tests already executed.

| Given / When | Then |
| --- | --- |
| Draft has missing refs, cycle, duplicate IDs, nonpositive estimate, or unreviewed/missing resources; activate | Reject; active curriculum/progress/queue unchanged. |
| Approved A incomplete; B depends on A; plan contains A | B excluded even if A's block comes first. |
| Started A, new B; model ranks B then A or invents X | Reject whole ranking; fallback started-first/curriculum order. |
| Root A only; 60 minutes remaining | Two distinct 30-minute blocks, not duplicate ranking IDs. |
| A,B; capacities 90 / 45 / 35 / 29 / 14 | A30 B30 A30 / A30 B15 / A30 plus5 unused / A29 / no block. |
| 60-minute budget, 25 actual today; /replan | One 30-minute block;5 unused. No catch-up debt. |
| A estimate exhausted but incomplete | Still eligible; no automatic completion or exclusion. |
| 30-minute block; /done confirms10 | Record10 once, close block, no topic completion/refill. |
| 60 budget, first block actual40, second pending30 | Preserve40; pending queue nonactionable; /replan can create20. |
| /done confirms0 or140 actual minutes | Record truthful time; zero doesn't start topic;140 stops further daily allocation. |
| Today's budget0 or remaining14 | Routine rest-day suppression/explanation as applicable; no undersized block. |
| Correct yesterday's session today | Update yesterday's effective total/audit, not today's capacity; max1440/date and no future date. |
| Two updates/clicks confirm same block/action | One effective session; return original operation result. |
| Stale confirmation after progress/curriculum/date/recovery change | Reject mutation, require fresh state/preview; no partial writes. |
| Reopen A with completed dependent B | Preview/confirm reopening both; preserve all original time. |
| Split A into B,C | Preserve A's minutes once; completion mappings explicit, never inferred by name. |
| Early /today then routine17:00; concurrent triggers | One logical routine presentation, no gratuitous reranking of valid queue. |
| Three missed days; recover after17:00 | Today's eligible queue only; no prior notifications/debt. |
| /done or new /replan occurs during AI call | Stale snapshot cannot overwrite current state or lose newer request generation. |
| Crash after acceptance before offset confirmation | Same update may reappear; same operation, no duplicate business effect. |
| Crash after plan/outbox commit before checkpoint | Recorded completion recognized; one plan/logical notification. |
| Crash during AI call, or model timeout/invalid schema | No second call for that job; validated fallback. |
| Old worker/poller/delivery fence after lease takeover | Cannot mutate protected DB records; no new dispatch from lost ownership. |
| Telegram accepted send, receipt lost | Retry may duplicate message, never progress or logical plan. |
| Telegram outage exceeds retry window | Failed record and external email; old-date messages never replay. |
| DB unavailable / model unavailable | No false success / fallback still operational, respectively. |
| Unauthorized user/group/callback/internal request | No learning-state change or secret exposure. |
| Two jobs compete for final daily/monthly allowance | Transactional reservation admits no over-limit AI dispatch. |
| Missing pricing, too many candidates/tokens, unknown model costs | Deterministic fallback, no unbounded request/spend. |
| /help and parser definitions compared | Exactly the eight documented commands/forms, including /progress. |
| External heartbeat/backup/disk/failure thresholds breached | Email alerts independent of VM/Telegram. |
| Restore off-VM backup into disposable environment | Verify curriculum/progress/audit, fresh epoch/lease invalidation, controlled delivery resumption, and 24-hour RPO/RTO targets. |

## Handoff exit condition

The owner approved this contract and confirmed no remaining product/architectural decision blocks implementation. PR #26 implements the bounded-ranking slice, not the full contract or production deployment. Operational credentials/vendors, live-provider verification, backup/restore, monitoring, and exact production readiness remain unverified. If implementation discovers incompatible source APIs or a requirement cannot be met, reopen the relevant decision rather than silently weakening its guarantee.
