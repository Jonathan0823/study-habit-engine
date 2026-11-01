import hashlib
import hmac
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.commands import COMMANDS, help_text
from app.dependencies import get_pool as shared_get_pool
from app.dependencies import required_config, set_pool


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["command"]
    command: str = Field(min_length=1, max_length=32)
    arguments: list[Annotated[str, Field(max_length=128)]] = Field(
        default_factory=list, max_length=8
    )


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: str = Field(pattern=r"^[0-9]{1,19}$")
    update_id: str = Field(pattern=r"^[0-9]{1,19}$")
    actor_id: str = Field(pattern=r"^[0-9]{1,19}$")
    chat_id: str = Field(pattern=r"^[0-9]{1,19}$")
    chat_type: Literal["private", "group", "supergroup", "channel"]
    poller_fence: int = Field(ge=1)
    payload: Payload


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: str
    owner_id: uuid.UUID


class DeliveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fence: int = Field(ge=1)
    outcome: Literal["sent", "transient_failure", "permanent_failure"]
    telegram_receipt: str | None = None


async def get_pool() -> asyncpg.Pool:
    return await shared_get_pool()


@asynccontextmanager
async def lifespan(_: FastAPI):
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        set_pool(await asyncpg.create_pool(dsn))
    try:
        yield
    finally:
        db = await shared_get_pool() if dsn else None
        if db is not None:
            await db.close()
            set_pool(None)


app = FastAPI(lifespan=lifespan)


def service_auth(authorization: str | None = Header(default=None)) -> None:
    expected = required_config("BOT_SERVICE_TOKEN")
    if authorization is None or not hmac.compare_digest(
        authorization, f"Bearer {expected}"
    ):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "retryable": False})


def parse_actual_minutes(arguments: list[str]) -> tuple[int | None, str | None]:
    if (
        len(arguments) != 1
        or not arguments[0].isascii()
        or not arguments[0].isdecimal()
    ):
        return None, "INVALID_ACTUAL_MINUTES"
    value = int(arguments[0])
    return (value, None) if 0 <= value <= 1440 else (None, "INVALID_ACTUAL_MINUTES")


def parse_budget(arguments: list[str]) -> tuple[int | None, str | None]:
    if not arguments:
        return None, "BUDGET_PROMPT"
    if (
        len(arguments) != 1
        or not arguments[0].isascii()
        or not arguments[0].isdecimal()
    ):
        return None, "INVALID_BUDGET"
    value = int(arguments[0])
    return (value, None) if 0 <= value <= 90 else (None, "INVALID_BUDGET")


async def lock_study_date_requests(conn, study_date) -> None:
    """Serialize queue creators without taking day before their job locks."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(0,$1::date - DATE '2000-01-01')",
        study_date,
    )


async def lock_replan_state(conn, study_date, budget_value: int | None = None):
    """Lock active jobs before the day, matching the worker's job -> day order."""
    old_jobs = await conn.fetch(
        "SELECT * FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running') FOR UPDATE",
        study_date,
    )
    day = await conn.fetchrow(
        "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", study_date
    )
    if budget_value is not None:
        await conn.execute(
            "UPDATE study_days SET budget_minutes=$2,state='replan_required' WHERE study_date=$1",
            study_date,
            budget_value,
        )
        await conn.execute(
            "UPDATE engine_state SET state_revision=state_revision+1 WHERE singleton=true"
        )
    return old_jobs, day


async def handle_block_command(conn, operation_id, command: str, arguments: list[str]):
    from datetime import datetime

    from app.planning import JAKARTA

    today = datetime.now(JAKARTA).date()
    await conn.execute(
        "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT(study_date) DO NOTHING",
        today,
    )
    # Match the planner's job -> day -> engine-state lock order.
    await conn.fetch(
        "SELECT job_id FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running') FOR UPDATE",
        today,
    )
    day = await conn.fetchrow(
        "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", today
    )
    await conn.fetchrow(
        "SELECT state_revision FROM engine_state WHERE singleton=true FOR UPDATE"
    )
    blocks = (
        await conn.fetch(
            """SELECT b.block_id,b.topic_id,b.planned_minutes,b.position
           FROM study_blocks b JOIN plans p USING(plan_id)
           WHERE p.plan_id=$1 AND p.study_date=$2 AND b.status='pending'
           ORDER BY b.position""",
            day["current_plan_id"],
            today,
        )
        if day["current_plan_id"] is not None and day["state"] == "valid"
        else []
    )
    target = None
    if arguments:
        try:
            target_id = uuid.UUID(arguments[0])
        except ValueError:
            target_id = None
        target = next(
            (block for block in blocks if block["block_id"] == target_id), None
        )
    if not arguments:
        if blocks:
            rows = [
                f"{i}. {row['topic_id']} — {row['planned_minutes']} min (block {row['block_id']})"
                for i, row in enumerate(blocks, 1)
            ]
            text = "Choose a current pending block:\n" + "\n".join(rows)
        else:
            text = "There are no current pending blocks. Use /today to view your queue."
        code = "BLOCK_SELECTION"
    elif command == "done" and len(arguments) == 1 and target is not None:
        text = (
            f"Confirm block {target['block_id']} ({target['topic_id']}) with "
            f"{target['planned_minutes']} minutes, or send /done {target['block_id']} "
            f"{day['queue_revision']} {target['planned_minutes']} (0–1440)."
        )
        code = "DONE_CONFIRMATION_REQUIRED"
    elif (
        command == "done"
        and len(arguments) == 3
        and (
            not arguments[1].isascii()
            or not arguments[1].isdecimal()
            or arguments[1] != str(day["queue_revision"])
        )
    ):
        text = "Today's queue changed since you selected this block. Use /done to choose again."
        code = "STALE_ACTION"
    elif command == "done" and len(arguments) == 3 and target is None:
        text = "That block is no longer pending in today's current queue. Use /today and choose again."
        code = "STALE_ACTION"
    elif (
        command == "done"
        and len(arguments) == 3
        and parse_actual_minutes([arguments[2]])[1]
    ):
        text = "Use /done followed by a block ID, queue revision, and actual minutes (0–1440)."
        code = "INVALID_ARGUMENTS"
    elif target is None:
        text = "That block is no longer pending in today's current queue. Use /today and choose again."
        code = "STALE_ACTION"
    elif command == "skip" and len(arguments) != 1:
        text = "Use /skip followed by one block ID, or /skip to choose a current pending block."
        code = "INVALID_ARGUMENTS"
    elif command == "done" and len(arguments) != 3:
        text = "Use /done followed by a block ID, queue revision, and actual minutes (0–1440)."
        code = "INVALID_ARGUMENTS"
    else:
        minutes = parse_actual_minutes([arguments[2]])[0] if command == "done" else None
        if command == "done":
            actual_today = await conn.fetchval(
                "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                today,
            )
            if actual_today + minutes > 1440:
                text, code = (
                    "Daily recorded study time cannot exceed 1440 minutes.",
                    "TIME_BOUNDS",
                )
            else:
                await conn.execute(
                    "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes,block_id) VALUES($1,$2,$3,$4,$5)",
                    uuid.uuid4(),
                    target["topic_id"],
                    today,
                    minutes,
                    target["block_id"],
                )
                await conn.execute(
                    "UPDATE study_blocks SET status='done' WHERE block_id=$1 AND status='pending'",
                    target["block_id"],
                )
                if minutes > 0:
                    await conn.execute(
                        "INSERT INTO topic_progress(topic_id,study_minutes) VALUES($1,$2) ON CONFLICT(topic_id) DO UPDATE SET study_minutes=topic_progress.study_minutes+$2",
                        target["topic_id"],
                        minutes,
                    )
                remaining_minutes = await conn.fetchval(
                    "SELECT COALESCE(sum(b.planned_minutes),0) FROM study_blocks b WHERE b.plan_id=$1 AND b.status='pending'",
                    day["current_plan_id"],
                )
                new_total = actual_today + minutes
                over_budget = remaining_minutes > max(
                    0, min(day["budget_minutes"], 90) - new_total
                )
                if over_budget:
                    await conn.execute(
                        "UPDATE study_days SET state='replan_required',queue_revision=queue_revision+1 WHERE study_date=$1",
                        today,
                    )
                    text = "Recorded and closed this block. The remaining queue exceeds today's available time; history is preserved. Use /replan for a replacement."
                    code = "REPLAN_REQUIRED"
                else:
                    await conn.execute(
                        "UPDATE study_days SET queue_revision=queue_revision+1 WHERE study_date=$1",
                        today,
                    )
                    text, code = (
                        f"Recorded {minutes} minutes and closed the selected block. The topic remains incomplete unless you mark it complete separately.",
                        "BLOCK_DONE",
                    )
                await conn.execute(
                    "UPDATE engine_state SET state_revision=state_revision+1 WHERE singleton=true"
                )
        else:
            await conn.execute(
                "UPDATE study_blocks SET status='skipped' WHERE block_id=$1 AND status='pending'",
                target["block_id"],
            )
            await conn.execute(
                "UPDATE study_days SET queue_revision=queue_revision+1 WHERE study_date=$1",
                today,
            )
            text, code = (
                "Skipped the selected block. No study time was recorded and the queue was not refilled.",
                "BLOCK_SKIPPED",
            )
    await conn.execute(
        "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code',$2::text)) WHERE operation_id=$1",
        operation_id,
        code,
    )
    await conn.execute(
        "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'done_skip',jsonb_build_object('text',$4::text)) ON CONFLICT(logical_key) DO NOTHING",
        uuid.uuid4(),
        f"operation:{operation_id}:done_skip",
        operation_id,
        text,
    )


def fixed_error(status: int, code: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"code": code, "retryable": retryable}
    )


@app.exception_handler(asyncpg.PostgresError)
async def postgres_error(_: Request, __: asyncpg.PostgresError):
    return fixed_error(503, "DATABASE_UNAVAILABLE", True)


@app.exception_handler(RuntimeError)
async def service_error(_: Request, __: RuntimeError):
    return fixed_error(503, "DATABASE_UNAVAILABLE", True)


@app.get("/health/live")
async def health_live():
    return {"status": "live"}


@app.get("/health/ready")
async def health_ready():
    for name in (
        "BOT_SERVICE_TOKEN",
        "TELEGRAM_BOT_ID",
        "TELEGRAM_USER_ID",
        "TELEGRAM_PRIVATE_CHAT_ID",
    ):
        required_config(name)
    db = await get_pool()
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ready"}


@app.post("/v1/telegram/claim", dependencies=[Depends(service_auth)])
async def claim(body: ClaimRequest):
    configured_bot = int(required_config("TELEGRAM_BOT_ID"))
    if int(body.bot_id) != configured_bot:
        raise HTTPException(403, detail={"code": "BOT_NOT_ALLOWED", "retryable": False})
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """INSERT INTO telegram_cursor(bot_id, next_offset, poller_owner, fence, lease_until)
               VALUES ($1, 0, $2, 1, now() + interval '60 seconds')
               ON CONFLICT (bot_id) DO UPDATE SET
                 fence = CASE WHEN telegram_cursor.poller_owner = EXCLUDED.poller_owner
                                   AND telegram_cursor.lease_until > now()
                              THEN telegram_cursor.fence ELSE telegram_cursor.fence + 1 END,
                 poller_owner = EXCLUDED.poller_owner,
                 lease_until = now() + interval '60 seconds'
               WHERE telegram_cursor.poller_owner = EXCLUDED.poller_owner
                  OR telegram_cursor.lease_until <= now()
               RETURNING next_offset, fence, lease_until""",
            configured_bot,
            body.owner_id,
        )
        if row is None:
            raise HTTPException(409, detail={"code": "POLLER_OWNED", "retryable": True})
        return {
            "next_offset": row["next_offset"],
            "fence": row["fence"],
            "lease_until": row["lease_until"].isoformat(),
        }


@app.post("/v1/telegram/updates", dependencies=[Depends(service_auth)])
async def ingest(body: Update):
    try:
        bot_id, update_id, actor_id, chat_id = map(
            int, (body.bot_id, body.update_id, body.actor_id, body.chat_id)
        )
    except ValueError:
        raise HTTPException(400, detail={"code": "INVALID_ID", "retryable": False})
    if (
        min(bot_id, update_id, actor_id, chat_id) < 0
        or max(bot_id, update_id, actor_id, chat_id) > 2**63 - 1
    ):
        raise HTTPException(400, detail={"code": "INVALID_ID", "retryable": False})
    configured_bot = int(required_config("TELEGRAM_BOT_ID"))
    source = f"telegram:{bot_id}:{update_id}"
    semantic = {
        "actor_id": body.actor_id,
        "chat_id": body.chat_id,
        "chat_type": body.chat_type,
        "payload": body.payload.model_dump(),
    }
    request_hash = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        duplicate = await conn.fetchrow(
            """SELECT o.operation_id, o.request_hash, o.result FROM telegram_ingestion i
               JOIN operations o USING (operation_id) WHERE i.bot_id=$1 AND i.update_id=$2""",
            bot_id,
            update_id,
        )
        if duplicate:
            if duplicate["request_hash"] != request_hash:
                raise HTTPException(
                    409, detail={"code": "IDEMPOTENCY_CONFLICT", "retryable": False}
                )
            saved_result = duplicate["result"]
            return (
                json.loads(saved_result)
                if isinstance(saved_result, str)
                else saved_result
            )

        cursor = await conn.fetchrow(
            "SELECT * FROM telegram_cursor WHERE bot_id=$1 FOR UPDATE", configured_bot
        )
        if (
            bot_id != configured_bot
            or cursor is None
            or cursor["fence"] != body.poller_fence
            or cursor["lease_until"] is None
            or cursor["lease_until"] <= await conn.fetchval("SELECT now()")
        ):
            raise HTTPException(409, detail={"code": "STALE_POLLER", "retryable": True})
        if update_id < cursor["next_offset"]:
            raise HTTPException(
                409, detail={"code": "OUT_OF_ORDER_UPDATE", "retryable": True}
            )

        authorized = (
            body.chat_type == "private"
            and actor_id == int(required_config("TELEGRAM_USER_ID"))
            and chat_id == int(required_config("TELEGRAM_PRIVATE_CHAT_ID"))
            and actor_id == chat_id
        )
        defined_command = any(name == body.payload.command for name, _, _ in COMMANDS)
        implemented_command = body.payload.command in (
            "help",
            "today",
            "replan",
            "budget",
            "done",
            "skip",
        )
        budget_value, budget_error = (
            parse_budget(body.payload.arguments)
            if body.payload.command == "budget"
            else (None, None)
        )
        _, actual_error = (
            parse_actual_minutes([body.payload.arguments[2]])
            if body.payload.command == "done" and len(body.payload.arguments) == 3
            else (None, None)
        )
        operation_id = uuid.uuid4()
        status = (
            "accepted"
            if authorized
            and implemented_command
            and budget_error != "INVALID_BUDGET"
            and not (
                body.payload.command == "done"
                and len(body.payload.arguments) == 3
                and actual_error
            )
            else "rejected"
        )
        result_code = (
            "UNAUTHORIZED"
            if not authorized
            else "UNSUPPORTED_COMMAND"
            if not defined_command
            else "NOT_IMPLEMENTED"
            if not implemented_command
            else budget_error
            if budget_error is not None
            else actual_error
            if body.payload.command == "done"
            and len(body.payload.arguments) == 3
            and actual_error is not None
            else "HELP_READY"
            if body.payload.command == "help"
            else "PLAN_ACCEPTED"
        )
        result = {
            "operation_id": str(operation_id),
            "status": status,
            "result": {"code": result_code},
            "callback_notice": "Accepted" if status == "accepted" else None,
        }
        await conn.execute(
            "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,$4,$5::jsonb)",
            operation_id,
            source,
            request_hash,
            status,
            json.dumps(result),
        )
        await conn.execute(
            "INSERT INTO telegram_ingestion(bot_id,update_id,operation_id,request_hash) VALUES($1,$2,$3,$4)",
            bot_id,
            update_id,
            operation_id,
            request_hash,
        )
        if status == "accepted" and body.payload.command == "help":
            await conn.execute(
                """INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload)
                   VALUES($1,$2,$3,'help',jsonb_build_object('text',$4::text))""",
                uuid.uuid4(),
                f"operation:{operation_id}:help",
                operation_id,
                help_text(),
            )
        elif status == "accepted" and body.payload.command in ("done", "skip"):
            await handle_block_command(
                conn,
                operation_id,
                body.payload.command,
                body.payload.arguments,
            )
        elif (
            status == "accepted"
            and body.payload.command == "budget"
            and budget_error == "BUDGET_PROMPT"
        ):
            await conn.execute(
                "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'help',jsonb_build_object('text',$4::text))",
                uuid.uuid4(),
                f"operation:{operation_id}:budget-prompt",
                operation_id,
                "Send /budget followed by a whole number from 0 to 90 minutes.",
            )
        elif status == "accepted" and body.payload.command in ("replan", "budget"):
            from datetime import datetime

            from app.planning import JAKARTA

            study_date = datetime.now(JAKARTA).date()
            await lock_study_date_requests(conn, study_date)
            await conn.execute(
                "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT(study_date) DO NOTHING",
                study_date,
            )
            # Keep all waiting operations attached to the newest generation. Locks
            # follow the worker's job -> day order, so a worker either commits before
            # this request or observes that its job was retired before doing work.
            old_jobs, day = await lock_replan_state(
                conn,
                study_date,
                budget_value if body.payload.command == "budget" else None,
            )
            generation = day["requested_generation"] + 1
            await conn.execute(
                "UPDATE study_days SET requested_generation=$2,state='replan_required' WHERE study_date=$1",
                study_date,
                generation,
            )
            for old_job in old_jobs:
                await conn.execute(
                    "UPDATE planning_jobs SET status='completed',result=$2::jsonb,lease_until=NULL WHERE job_id=$1",
                    old_job["job_id"],
                    json.dumps({"reason": "COALESCED", "generation": generation}),
                )
            job_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO planning_jobs(job_id,operation_id,study_date,generation) VALUES($1,$2,$3,$4)",
                job_id,
                operation_id,
                study_date,
                generation,
            )
            for old_job in old_jobs:
                await conn.execute(
                    "UPDATE planning_job_operations SET job_id=$2 WHERE job_id=$1",
                    old_job["job_id"],
                    job_id,
                )
            await conn.execute(
                "INSERT INTO planning_job_operations(operation_id,job_id) VALUES($1,$2)",
                operation_id,
                job_id,
            )
        elif status == "accepted" and body.payload.command == "today":
            from datetime import datetime

            from app.planning import JAKARTA

            study_date = datetime.now(JAKARTA).date()
            await lock_study_date_requests(conn, study_date)
            day = await conn.fetchrow(
                """INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT(study_date)
                   DO UPDATE SET study_date=EXCLUDED.study_date RETURNING *""",
                study_date,
            )
            if day["state"] == "replan_required":
                await conn.execute(
                    "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code','REPLAN_REQUIRED')) WHERE operation_id=$1",
                    operation_id,
                )
                await conn.execute(
                    "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'today',jsonb_build_object('text',$4::text)) ON CONFLICT(logical_key) DO NOTHING",
                    uuid.uuid4(),
                    f"operation:{operation_id}:today",
                    operation_id,
                    "Today's queue needs to be refreshed. Use /replan to create a replacement queue.",
                )
            else:
                if day["state"] == "valid" and day["current_plan_id"] is not None:
                    plan = await conn.fetchrow(
                        """SELECT p.curriculum_id,p.state_revision,v.status,v.content
                           FROM plans p JOIN curriculum_versions v ON v.curriculum_id=p.curriculum_id
                           JOIN engine_state s ON s.singleton=true
                           WHERE p.plan_id=$1 AND p.study_date=$2 AND p.curriculum_id=s.active_curriculum_id
                             AND p.state_revision=s.state_revision AND v.status='approved'""",
                        day["current_plan_id"],
                        study_date,
                    )
                    rows = (
                        await conn.fetch(
                            "SELECT topic_id,planned_minutes FROM study_blocks WHERE plan_id=$1 AND status='pending' ORDER BY position",
                            day["current_plan_id"],
                        )
                        if plan
                        else []
                    )
                    content = plan["content"] if plan else None
                    if isinstance(content, str):
                        content = json.loads(content)
                    progress = await conn.fetch(
                        "SELECT topic_id FROM topic_progress WHERE completed=true"
                    )
                    completed = {row["topic_id"] for row in progress}
                    from app.planning import render_queue

                    actual_today = await conn.fetchval(
                        "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                        study_date,
                    )
                    display_lines = (
                        render_queue(
                            rows,
                            content,
                            completed,
                            max(0, day["budget_minutes"] - actual_today),
                        )
                        if content
                        else None
                    )
                    if display_lines is not None:
                        await conn.execute(
                            "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code','PLAN_READY','plan_id',$2::text)) WHERE operation_id=$1",
                            operation_id,
                            str(day["current_plan_id"]),
                        )
                        await conn.execute(
                            "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'today',jsonb_build_object('text',$4::text)) ON CONFLICT(logical_key) DO NOTHING",
                            uuid.uuid4(),
                            f"operation:{operation_id}:today",
                            operation_id,
                            "\n".join(display_lines),
                        )
                    else:
                        await conn.execute(
                            "UPDATE study_days SET state='replan_required' WHERE study_date=$1",
                            study_date,
                        )
                        day = dict(day)
                        day["state"] = "replan_required"
                if day["state"] != "valid":
                    active_job = await conn.fetchrow(
                        "SELECT job_id FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running')",
                        study_date,
                    )
                    if active_job is not None:
                        await conn.execute(
                            "INSERT INTO planning_job_operations(operation_id,job_id) VALUES($1,$2)",
                            operation_id,
                            active_job["job_id"],
                        )
                    else:
                        generation = day["requested_generation"] + 1
                        await conn.execute(
                            "UPDATE study_days SET requested_generation=$2 WHERE study_date=$1",
                            study_date,
                            generation,
                        )
                        job_id = uuid.uuid4()
                        await conn.execute(
                            "INSERT INTO planning_jobs(job_id,operation_id,study_date,generation) VALUES($1,$2,$3,$4)",
                            job_id,
                            operation_id,
                            study_date,
                            generation,
                        )
                        await conn.execute(
                            "INSERT INTO planning_job_operations(operation_id,job_id) VALUES($1,$2)",
                            operation_id,
                            job_id,
                        )
        await conn.execute(
            "UPDATE telegram_cursor SET next_offset=$2, lease_until=now()+interval '60 seconds' WHERE bot_id=$1",
            configured_bot,
            update_id + 1,
        )
        return result


@app.post("/v1/deliveries/claim", dependencies=[Depends(service_auth)])
async def claim_delivery():
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE deliveries SET status='failed',lease_until=NULL WHERE status IN ('pending','leased') AND expires_at <= now()"
        )
        row = await conn.fetchrow(
            """SELECT d.delivery_id,d.operation_id,d.payload,d.fence FROM deliveries d
               WHERE d.expires_at > now() AND (d.status='pending' AND d.next_attempt_at <= now()
                  OR d.status='leased' AND d.lease_until < now())
               ORDER BY d.created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
        )
        if row is None:
            return JSONResponse(status_code=204, content=None)
        leased = await conn.fetchrow(
            """UPDATE deliveries SET status='leased', attempts=attempts+1, fence=fence+1,
               lease_until=now()+interval '60 seconds' WHERE delivery_id=$1
               RETURNING fence,lease_until""",
            row["delivery_id"],
        )
        payload = json.loads(row["payload"])
        return {
            "delivery_id": str(row["delivery_id"]),
            "operation_id": str(row["operation_id"]),
            "fence": leased["fence"],
            "lease_until": leased["lease_until"].isoformat(),
            "chat_id": required_config("TELEGRAM_PRIVATE_CHAT_ID"),
            "text": payload["text"],
        }


@app.post("/v1/deliveries/{delivery_id}/result", dependencies=[Depends(service_auth)])
async def delivery_result(delivery_id: uuid.UUID, body: DeliveryResult):
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT status,fence,lease_until FROM deliveries WHERE delivery_id=$1 FOR UPDATE",
            delivery_id,
        )
        if row is None:
            raise HTTPException(
                404, detail={"code": "DELIVERY_NOT_FOUND", "retryable": False}
            )
        if row["status"] == "sent":
            return {"status": "sent"}
        if (
            row["status"] != "leased"
            or row["fence"] != body.fence
            or row["lease_until"] is None
            or row["lease_until"] <= await conn.fetchval("SELECT now()")
        ):
            raise HTTPException(
                409, detail={"code": "STALE_DELIVERY", "retryable": True}
            )
        if body.outcome == "sent":
            if body.telegram_receipt is None:
                raise HTTPException(
                    400, detail={"code": "RECEIPT_REQUIRED", "retryable": False}
                )
            status = "sent"
        elif body.outcome == "transient_failure":
            status = "pending"
        else:
            status = "failed"
        if body.outcome == "transient_failure":
            await conn.execute(
                """UPDATE deliveries SET status=CASE WHEN expires_at <= now() + make_interval(secs =>
                       LEAST(5 * power(2, LEAST(attempts,6)),300)::integer) THEN 'failed' ELSE 'pending' END,
                   next_attempt_at=now()+make_interval(secs => LEAST(5 * power(2, LEAST(attempts,6)),300)::integer),
                   lease_until=NULL WHERE delivery_id=$1""",
                delivery_id,
            )
        else:
            await conn.execute(
                "UPDATE deliveries SET status=$2,telegram_receipt=$3,lease_until=NULL WHERE delivery_id=$1",
                delivery_id,
                status,
                body.telegram_receipt,
            )
        return {"status": status}


from app.curriculum import confirmation_router
from app.curriculum import router as curriculum_router

app.include_router(curriculum_router)
app.include_router(confirmation_router)
