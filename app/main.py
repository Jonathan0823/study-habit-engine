import hashlib
import hmac
import json
import os
import secrets
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
    """Lock active jobs, engine state, then the day, matching the worker."""
    old_jobs = await conn.fetch(
        "SELECT * FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running') FOR UPDATE",
        study_date,
    )
    await conn.fetchrow("SELECT * FROM engine_state WHERE singleton=true FOR UPDATE")
    await conn.execute(
        "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT DO NOTHING",
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


def completed_dependents(topic_id, topics, completed):
    dependents = set()
    frontier = {topic_id}
    while frontier:
        following = {
            tid
            for tid, topic in topics.items()
            if tid in completed
            and tid not in dependents
            and frontier.intersection(topic["prerequisites"])
        }
        dependents.update(following)
        frontier = following
    return dependents


async def issue_action_confirmation(
    conn,
    actor_id,
    chat_id,
    action,
    target,
    study_date,
    state_revision,
    queue_revision,
    recovery_epoch,
    planned_minutes=None,
    context=None,
):
    token = secrets.token_urlsafe(32)
    await conn.execute(
        """INSERT INTO action_confirmations(token,actor_id,chat_id,recovery_epoch,action,target,study_date,state_revision,queue_revision,planned_minutes,expires_at,context)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now()+interval '10 minutes',$11::jsonb)""",
        token,
        actor_id,
        chat_id,
        recovery_epoch,
        action,
        str(target),
        study_date,
        state_revision,
        queue_revision,
        planned_minutes,
        json.dumps(context or {}),
    )
    return token


async def handle_topic_command(
    conn,
    operation_id,
    arguments: list[str],
    actor_id: int,
    chat_id: int,
    token_action: str | None = None,
):
    from datetime import datetime

    from app.planning import JAKARTA

    today = datetime.now(JAKARTA).date()
    await lock_study_date_requests(conn, today)
    await conn.fetch(
        "SELECT job_id FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running') FOR UPDATE",
        today,
    )
    state = await conn.fetchrow(
        "SELECT * FROM engine_state WHERE singleton=true FOR UPDATE"
    )
    await conn.execute(
        "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT DO NOTHING", today
    )
    day = await conn.fetchrow(
        "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", today
    )
    enqueue_delivery = True
    if state["active_curriculum_id"] is None:
        text, code = "There is no active curriculum.", "NO_ACTIVE_CURRICULUM"
    else:
        curriculum = await conn.fetchrow(
            "SELECT content FROM curriculum_versions WHERE curriculum_id=$1 AND status='approved'",
            state["active_curriculum_id"],
        )
        content = (
            curriculum["content"]
            if isinstance(curriculum["content"], dict)
            else json.loads(curriculum["content"])
        )
        topics = {topic["topic_id"]: topic for topic in content["topics"]}
        progress_rows = await conn.fetch(
            "SELECT topic_id,completed FROM topic_progress"
        )
        completed = {r["topic_id"] for r in progress_rows if r["completed"]}
        if not arguments:
            eligible = [
                t["topic_id"]
                for t in content["topics"]
                if t["topic_id"] not in completed
                and set(t["prerequisites"]) <= completed
            ]
            text, code = (
                ("Eligible topics: " + ", ".join(eligible), "TOPIC_SELECTION")
                if eligible
                else ("No eligible topics to complete.", "NO_ELIGIBLE_TOPICS")
            )
        elif len(arguments) == 1 and arguments[0] in topics:
            topic_id = arguments[0]
            if topic_id in completed:
                dependents = completed_dependents(topic_id, topics, completed)
                token = await issue_action_confirmation(
                    conn,
                    actor_id,
                    chat_id,
                    "topic_reopen",
                    topic_id,
                    today,
                    state["state_revision"],
                    day["queue_revision"],
                    state["recovery_epoch"],
                )
                text = f"Reopening {topic_id} will also reopen completed dependents: {', '.join(sorted(dependents)) or 'none'}. Confirm with /complete_topic {token}."
                code = "REOPEN_CONFIRMATION_REQUIRED"
            elif not set(topics[topic_id]["prerequisites"]) <= completed:
                text, code = (
                    "This topic is not eligible until all prerequisites are complete.",
                    "TOPIC_NOT_ELIGIBLE",
                )
            else:
                token = await issue_action_confirmation(
                    conn,
                    actor_id,
                    chat_id,
                    "topic_complete",
                    topic_id,
                    today,
                    state["state_revision"],
                    day["queue_revision"],
                    state["recovery_epoch"],
                )
                text = f"Complete {topic_id}? Confirm with /complete_topic {token}."
                code = "TOPIC_CONFIRMATION_REQUIRED"
        elif len(arguments) == 1 and arguments[0] not in topics:
            confirmation = await conn.fetchrow(
                "SELECT * FROM action_confirmations WHERE token=$1 FOR UPDATE",
                arguments[0],
            )
            if confirmation is None or confirmation["action"] not in (
                "topic_complete",
                "topic_reopen",
            ):
                text, code = (
                    "Confirmation is invalid or expired. Preview again.",
                    "STALE_ACTION",
                )
            elif (
                confirmation["actor_id"] != actor_id
                or confirmation["chat_id"] != chat_id
                or confirmation["recovery_epoch"] != state["recovery_epoch"]
            ):
                text, code = (
                    "Study state changed. Use /complete_topic to preview again.",
                    "STALE_ACTION",
                )
            elif confirmation["consumed_at"] is not None:
                text, code = confirmation["result_text"], confirmation["result_code"]
                enqueue_delivery = False
            elif (
                confirmation["study_date"] != today
                or confirmation["expires_at"] <= await conn.fetchval("SELECT now()")
                or confirmation["state_revision"] != state["state_revision"]
                or confirmation["queue_revision"] != day["queue_revision"]
                or confirmation["target"] not in topics
            ):
                text, code = (
                    "Study state changed. Use /complete_topic to preview again.",
                    "STALE_ACTION",
                )
            else:
                await handle_topic_command(
                    conn,
                    operation_id,
                    [
                        confirmation["target"],
                        str(confirmation["state_revision"]),
                        str(confirmation["queue_revision"]),
                        "confirm",
                    ],
                    actor_id,
                    chat_id,
                    token_action=confirmation["action"],
                )
                saved = await conn.fetchrow(
                    "SELECT result FROM operations WHERE operation_id=$1", operation_id
                )
                delivery = await conn.fetchval(
                    "SELECT payload->>'text' FROM deliveries WHERE logical_key=$1",
                    f"operation:{operation_id}:topic",
                )
                saved_result = (
                    saved["result"]
                    if isinstance(saved["result"], dict)
                    else json.loads(saved["result"])
                )
                text, code = delivery, saved_result["result"]["code"]
                await conn.execute(
                    "UPDATE action_confirmations SET consumed_at=now(),result_code=$2,result_text=$3 WHERE token=$1",
                    arguments[0],
                    code,
                    text,
                )
        elif len(arguments) == 4 and arguments[3] == "confirm":
            topic_id = arguments[0]
            if token_action not in ("topic_complete", "topic_reopen"):
                text, code = (
                    "Use the tokenized confirmation shown in the preview.",
                    "INVALID_ARGUMENTS",
                )
                topic_id = None
            revision, queue_revision = arguments[1:3]
            if (
                token_action not in ("topic_complete", "topic_reopen")
                or topic_id not in topics
                or not revision.isascii()
                or not revision.isdecimal()
                or not queue_revision.isascii()
                or not queue_revision.isdecimal()
                or int(revision) != state["state_revision"]
                or int(queue_revision) != day["queue_revision"]
            ):
                text, code = (
                    "Study state changed. Use /complete_topic to preview again.",
                    "STALE_ACTION",
                )
            elif (token_action == "topic_reopen" and topic_id not in completed) or (
                token_action == "topic_complete" and topic_id in completed
            ):
                text, code = (
                    "Topic state changed. Use /complete_topic to preview again.",
                    "STALE_ACTION",
                )
            elif token_action == "topic_reopen":
                reopened = {topic_id} | completed_dependents(
                    topic_id, topics, completed
                )
                await conn.execute(
                    """INSERT INTO topic_progress_audit(operation_id,topic_id,before_state,after_state)
                       SELECT $1,topic_id,
                              jsonb_build_object('completed',completed,'study_minutes',study_minutes),
                              jsonb_build_object('completed',false,'study_minutes',study_minutes)
                       FROM topic_progress WHERE topic_id=ANY($2::text[])""",
                    operation_id,
                    list(reopened),
                )
                await conn.execute(
                    "UPDATE topic_progress SET completed=false WHERE topic_id=ANY($1::text[])",
                    list(reopened),
                )
                completed_after_reopen = completed - reopened
                pending_blocks = []
                if day["current_plan_id"] is not None:
                    pending_blocks = await conn.fetch(
                        "SELECT block_id,topic_id FROM study_blocks WHERE plan_id=$1 AND status='pending'",
                        day["current_plan_id"],
                    )
                invalid_pending = {
                    block["topic_id"]
                    for block in pending_blocks
                    if block["topic_id"] not in topics
                    or block["topic_id"] in completed_after_reopen
                    or not set(topics[block["topic_id"]]["prerequisites"])
                    <= completed_after_reopen
                }
                cancelled_topics = invalid_pending
                if day["current_plan_id"] is not None and cancelled_topics:
                    await conn.execute(
                        "UPDATE study_blocks SET status='cancelled' WHERE plan_id=$1 AND status='pending' AND topic_id=ANY($2::text[])",
                        day["current_plan_id"],
                        list(cancelled_topics),
                    )
                next_state = "replan_required" if invalid_pending else day["state"]
                await conn.execute(
                    "UPDATE study_days SET state=$2,queue_revision=queue_revision+1 WHERE study_date=$1",
                    today,
                    next_state,
                )
                await conn.execute(
                    "UPDATE engine_state SET state_revision=state_revision+1,progress_revision=progress_revision+1 WHERE singleton=true"
                )
                if (
                    next_state != "replan_required"
                    and day["current_plan_id"] is not None
                ):
                    await conn.execute(
                        "UPDATE plans SET state_revision=(SELECT state_revision FROM engine_state WHERE singleton=true) WHERE plan_id=$1",
                        day["current_plan_id"],
                    )
                text, code = (
                    f"Reopened {', '.join(sorted(reopened))}. Recorded study time and history were preserved; use /replan to refresh the queue.",
                    "TOPICS_REOPENED",
                )
            elif (
                token_action != "topic_complete"
                or not set(topics[topic_id]["prerequisites"]) <= completed
            ):
                text, code = (
                    "Prerequisites changed. Use /complete_topic to preview again.",
                    "STALE_ACTION",
                )
            else:
                await conn.execute(
                    """INSERT INTO topic_progress_audit(operation_id,topic_id,before_state,after_state)
                       SELECT $1,$2,
                              jsonb_build_object('completed',COALESCE(progress.completed,false),
                                                 'study_minutes',COALESCE(progress.study_minutes,0)),
                              jsonb_build_object('completed',true,
                                                 'study_minutes',COALESCE(progress.study_minutes,0))
                       FROM (SELECT 1) seed
                       LEFT JOIN topic_progress progress ON progress.topic_id=$2""",
                    operation_id,
                    topic_id,
                )
                await conn.execute(
                    "INSERT INTO topic_progress(topic_id,completed) VALUES($1,true) ON CONFLICT(topic_id) DO UPDATE SET completed=true",
                    topic_id,
                )
                if day["current_plan_id"] is not None:
                    await conn.execute(
                        "UPDATE study_blocks SET status='cancelled' WHERE plan_id=$1 AND status='pending' AND topic_id=$2",
                        day["current_plan_id"],
                        topic_id,
                    )
                await conn.execute(
                    "UPDATE study_days SET queue_revision=queue_revision+1 WHERE study_date=$1",
                    today,
                )
                await conn.execute(
                    "UPDATE engine_state SET state_revision=state_revision+1,progress_revision=progress_revision+1 WHERE singleton=true"
                )
                if (
                    day["state"] != "replan_required"
                    and day["current_plan_id"] is not None
                ):
                    await conn.execute(
                        "UPDATE plans SET state_revision=(SELECT state_revision FROM engine_state WHERE singleton=true) WHERE plan_id=$1",
                        day["current_plan_id"],
                    )
                text, code = (
                    f"Completed {topic_id}. The queue was not refilled.",
                    "TOPIC_COMPLETED",
                )
        else:
            text, code = (
                "Use /complete_topic [topic_id] to preview, then confirm with the shown state and queue revisions.",
                "INVALID_ARGUMENTS",
            )
    await conn.execute(
        "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code',$2::text)) WHERE operation_id=$1",
        operation_id,
        code,
    )
    if enqueue_delivery:
        await conn.execute(
            "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'topic',jsonb_build_object('text',$4::text)) ON CONFLICT(logical_key) DO NOTHING",
            uuid.uuid4(),
            f"operation:{operation_id}:topic",
            operation_id,
            text,
        )


async def handle_progress_command(conn, operation_id, arguments, actor_id, chat_id):
    from datetime import date, datetime

    from app.planning import JAKARTA

    today = datetime.now(JAKARTA).date()
    mode = (
        arguments[0]
        if arguments and arguments[0] in {"add", "edit", "reopen", "confirm"}
        else "review"
    )
    token = arguments[1] if mode == "confirm" and len(arguments) == 2 else None
    confirmation = None
    if mode != "review":
        lock_dates = {today}
        if mode == "confirm" and token is not None:
            # Inspect the token without a row lock so date advisory locks always
            # precede confirmation and business-row locks.
            preview = await conn.fetchrow(
                "SELECT context FROM action_confirmations WHERE token=$1", token
            )
            if preview is not None:
                context = preview["context"]
                context = context if isinstance(context, dict) else json.loads(context)
                if context.get("study_date"):
                    lock_dates.add(date.fromisoformat(context["study_date"]))
                if context.get("new_date"):
                    lock_dates.add(date.fromisoformat(context["new_date"]))
        elif mode == "add" and len(arguments) in (3, 4):
            try:
                lock_dates.add(
                    date.fromisoformat(
                        arguments[3] if len(arguments) == 4 else today.isoformat()
                    )
                )
            except ValueError:
                pass
        elif mode == "edit" and len(arguments) == 4:
            try:
                lock_dates.add(date.fromisoformat(arguments[2]))
                session_id = uuid.UUID(arguments[1])
                before = await conn.fetchrow(
                    "SELECT study_date FROM study_sessions WHERE session_id=$1",
                    session_id,
                )
                if before is not None:
                    lock_dates.add(before["study_date"])
            except ValueError:
                pass
        for lock_day in sorted(lock_dates):
            await lock_study_date_requests(conn, lock_day)
        for lock_day in sorted(lock_dates):
            await conn.fetch(
                "SELECT job_id FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running') FOR UPDATE",
                lock_day,
            )
        state = await conn.fetchrow(
            "SELECT * FROM engine_state WHERE singleton=true FOR UPDATE"
        )
        await conn.execute(
            "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT DO NOTHING",
            today,
        )
        for lock_day in sorted(lock_dates):
            await conn.fetchrow(
                "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", lock_day
            )
        if token is not None:
            confirmation = await conn.fetchrow(
                "SELECT * FROM action_confirmations WHERE token=$1 FOR UPDATE", token
            )
    if token is not None:
        if confirmation and confirmation["consumed_at"] is not None:
            if (
                confirmation["actor_id"] != actor_id
                or confirmation["chat_id"] != chat_id
                or confirmation["recovery_epoch"] != state["recovery_epoch"]
            ):
                text, code = (
                    "Confirmation is invalid for this user or chat.",
                    "STALE_ACTION",
                )
            else:
                text, code = confirmation["result_text"], confirmation["result_code"]
            await conn.execute(
                "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code',$2::text)) WHERE operation_id=$1",
                operation_id,
                code,
            )
            await conn.execute(
                "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'progress',jsonb_build_object('text',$4::text)) ON CONFLICT(logical_key) DO NOTHING",
                uuid.uuid4(),
                f"operation:{operation_id}:progress",
                operation_id,
                text,
            )
            return
        if confirmation is None or confirmation["action"] not in (
            "progress_add",
            "progress_edit",
            "progress_reopen",
        ):
            text, code = (
                "Confirmation is invalid or expired. Preview the action again.",
                "STALE_ACTION",
            )
        elif (
            confirmation["actor_id"] != actor_id
            or confirmation["chat_id"] != chat_id
            or confirmation["recovery_epoch"] != state["recovery_epoch"]
            or confirmation["expires_at"] <= await conn.fetchval("SELECT now()")
            or confirmation["state_revision"] != state["state_revision"]
        ):
            text, code = (
                "Study state changed. Preview the action again.",
                "STALE_ACTION",
            )
        else:
            mode = confirmation["action"]
            context = (
                confirmation["context"]
                if isinstance(confirmation["context"], dict)
                else json.loads(confirmation["context"])
            )
            target_date = date.fromisoformat(context["study_date"])
            current_queue_revision = await conn.fetchval(
                "SELECT COALESCE((SELECT queue_revision FROM study_days WHERE study_date=$1),0)",
                target_date,
            )
            new_queue_revision = current_queue_revision
            if "new_date" in context:
                new_queue_revision = await conn.fetchval(
                    "SELECT COALESCE((SELECT queue_revision FROM study_days WHERE study_date=$1),0)",
                    date.fromisoformat(context["new_date"]),
                )
            if confirmation[
                "queue_revision"
            ] != current_queue_revision or new_queue_revision != context.get(
                "new_queue_revision", new_queue_revision
            ):
                text, code = (
                    "The target day changed. Preview the action again.",
                    "STALE_ACTION",
                )
            elif target_date > today:
                text, code = "Future dates cannot be recorded.", "INVALID_DATE"
            elif mode in ("progress_add", "progress_edit"):
                new_day = date.fromisoformat(context["new_date"])
                for lock_day in sorted({target_date, new_day}):
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(0,$1::date - DATE '2000-01-01')",
                        lock_day,
                    )
                session_id = uuid.UUID(context["session_id"])
                before = (
                    await conn.fetchrow(
                        "SELECT * FROM study_sessions WHERE session_id=$1 FOR UPDATE",
                        session_id,
                    )
                    if mode == "progress_edit"
                    else None
                )
                if mode == "progress_edit" and (
                    before is None
                    or before["topic_id"] != context["topic_id"]
                    or before["study_date"].isoformat() != context["before_date"]
                    or before["actual_minutes"] != context["before_minutes"]
                ):
                    text, code = (
                        "This session changed. Review history and preview the correction again.",
                        "STALE_ACTION",
                    )
                else:
                    new_day = date.fromisoformat(context["new_date"])
                    prior_total = await conn.fetchval(
                        "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                        new_day,
                    )
                    effective = (
                        prior_total
                        - (
                            before["actual_minutes"]
                            if before and before["study_date"] == new_day
                            else 0
                        )
                        + context["minutes"]
                    )
                    if effective > 1440:
                        text, code = (
                            "The corrected daily total would exceed 1440 minutes.",
                            "DAILY_LIMIT_EXCEEDED",
                        )
                    else:
                        if before:
                            await conn.execute(
                                "UPDATE study_sessions SET study_date=$2,actual_minutes=$3 WHERE session_id=$1",
                                session_id,
                                new_day,
                                context["minutes"],
                            )
                        else:
                            await conn.execute(
                                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,$2,$3,$4)",
                                session_id,
                                context["topic_id"],
                                new_day,
                                context["minutes"],
                            )
                        after = {
                            "topic_id": context["topic_id"],
                            "study_date": new_day.isoformat(),
                            "actual_minutes": context["minutes"],
                        }
                        before_json = (
                            json.dumps(
                                {
                                    "topic_id": before["topic_id"],
                                    "study_date": before["study_date"].isoformat(),
                                    "actual_minutes": before["actual_minutes"],
                                }
                            )
                            if before
                            else None
                        )
                        await conn.execute(
                            "INSERT INTO study_session_audit(operation_id,session_id,action,before_state,after_state) VALUES($1,$2,$3,$4::jsonb,$5::jsonb)",
                            operation_id,
                            session_id,
                            "correct" if before else "add",
                            before_json,
                            json.dumps(after),
                        )
                        await conn.execute(
                            "INSERT INTO topic_progress(topic_id,study_minutes) VALUES($1,$2) ON CONFLICT(topic_id) DO UPDATE SET study_minutes=(SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE topic_id=$1)",
                            context["topic_id"],
                            context["minutes"],
                        )
                        replan_required = False
                        if new_day == today or (
                            before and before["study_date"] == today
                        ):
                            await conn.execute(
                                "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT DO NOTHING",
                                today,
                            )
                            today_row = await conn.fetchrow(
                                "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE",
                                today,
                            )
                            actual_today = await conn.fetchval(
                                "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                                today,
                            )
                            pending = (
                                await conn.fetch(
                                    "SELECT topic_id,planned_minutes FROM study_blocks WHERE plan_id=$1 AND status='pending'",
                                    today_row["current_plan_id"],
                                )
                                if today_row["current_plan_id"] is not None
                                else []
                            )
                            pending_minutes = sum(
                                row["planned_minutes"] for row in pending
                            )
                            completed_today = {
                                row["topic_id"]
                                for row in await conn.fetch(
                                    "SELECT topic_id FROM topic_progress WHERE completed=true"
                                )
                            }
                            current_content = (
                                await conn.fetchval(
                                    "SELECT content FROM curriculum_versions WHERE curriculum_id=$1 AND status='approved'",
                                    state["active_curriculum_id"],
                                )
                                if state["active_curriculum_id"]
                                else None
                            )
                            if current_content is not None:
                                current_content = (
                                    current_content
                                    if isinstance(current_content, dict)
                                    else json.loads(current_content)
                                )
                                current_topics = {
                                    topic["topic_id"]: topic
                                    for topic in current_content["topics"]
                                }
                                handoff_invalid = any(
                                    block["topic_id"] not in current_topics
                                    or block["topic_id"] in completed_today
                                    or not set(
                                        current_topics[block["topic_id"]][
                                            "prerequisites"
                                        ]
                                    )
                                    <= completed_today
                                    for block in pending
                                )
                            else:
                                handoff_invalid = bool(pending)
                            remaining = max(
                                0, today_row["budget_minutes"] - actual_today
                            )
                            replan_required = (
                                remaining < pending_minutes or handoff_invalid
                            )
                            if replan_required:
                                await conn.execute(
                                    "UPDATE study_days SET state='replan_required',queue_revision=queue_revision+1 WHERE study_date=$1",
                                    today,
                                )
                        past_progress_staled_plan = False
                        if new_day != today:
                            today_row = await conn.fetchrow(
                                "SELECT current_plan_id FROM study_days WHERE study_date=$1",
                                today,
                            )
                            if today_row and today_row["current_plan_id"] is not None:
                                plan_revision = await conn.fetchval(
                                    "SELECT state_revision FROM plans WHERE plan_id=$1",
                                    today_row["current_plan_id"],
                                )
                                past_progress_staled_plan = (
                                    plan_revision is not None
                                    and plan_revision != state["state_revision"] + 1
                                )
                                if past_progress_staled_plan and not replan_required:
                                    await conn.execute(
                                        "UPDATE study_days SET state='replan_required',queue_revision=queue_revision+1 WHERE study_date=$1",
                                        today,
                                    )
                                    replan_required = True
                        await conn.execute(
                            "UPDATE engine_state SET state_revision=state_revision+1,progress_revision=progress_revision+1 WHERE singleton=true"
                        )
                        if not replan_required:
                            current_day = await conn.fetchrow(
                                "SELECT state,current_plan_id FROM study_days WHERE study_date=$1",
                                today,
                            )
                            if (
                                current_day["state"] != "replan_required"
                                and current_day["current_plan_id"] is not None
                            ):
                                await conn.execute(
                                    "UPDATE plans SET state_revision=(SELECT state_revision FROM engine_state WHERE singleton=true) WHERE plan_id=$1",
                                    current_day["current_plan_id"],
                                )
                        if replan_required and past_progress_staled_plan:
                            text, code = (
                                f"Recorded session {session_id} for {new_day}: {context['minutes']} minutes. Today's plan is stale because historical progress changed; queue history is preserved. Use /replan to refresh it.",
                                "REPLAN_REQUIRED",
                            )
                        elif replan_required:
                            text, code = (
                                f"Recorded session {session_id} for {new_day}: {context['minutes']} minutes. Today's remaining capacity is below the pending queue or the queue no longer satisfies topic prerequisites; queue history is preserved. Use /replan to refresh it.",
                                "REPLAN_REQUIRED",
                            )
                        else:
                            text, code = (
                                f"Recorded session {session_id} for {new_day}: {context['minutes']} minutes.",
                                "PROGRESS_RECORDED",
                            )
            else:
                topic_id = confirmation["target"]
                current = (
                    await conn.fetchrow(
                        "SELECT content FROM curriculum_versions WHERE curriculum_id=$1 AND status='approved'",
                        state["active_curriculum_id"],
                    )
                    if state["active_curriculum_id"]
                    else None
                )
                if current is None:
                    text, code = (
                        "There is no active curriculum.",
                        "NO_ACTIVE_CURRICULUM",
                    )
                else:
                    content = (
                        current["content"]
                        if isinstance(current["content"], dict)
                        else json.loads(current["content"])
                    )
                    topics = {t["topic_id"]: t for t in content["topics"]}
                    completed = {
                        r["topic_id"]
                        for r in await conn.fetch(
                            "SELECT topic_id FROM topic_progress WHERE completed=true"
                        )
                    }
                    if topic_id not in topics or topic_id not in completed:
                        text, code = (
                            "Topic state changed. Preview again.",
                            "STALE_ACTION",
                        )
                    else:
                        reopened = {topic_id} | completed_dependents(
                            topic_id, topics, completed
                        )
                        await conn.execute(
                            """INSERT INTO topic_progress_audit(operation_id,topic_id,before_state,after_state)
                               SELECT $1,topic_id,
                                      jsonb_build_object('completed',completed,'study_minutes',study_minutes),
                                      jsonb_build_object('completed',false,'study_minutes',study_minutes)
                               FROM topic_progress WHERE topic_id=ANY($2::text[])""",
                            operation_id,
                            list(reopened),
                        )
                        await conn.execute(
                            "UPDATE topic_progress SET completed=false WHERE topic_id=ANY($1::text[])",
                            list(reopened),
                        )
                        await conn.execute(
                            "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT DO NOTHING",
                            today,
                        )
                        day = await conn.fetchrow(
                            "SELECT * FROM study_days WHERE study_date=$1",
                            today,
                        )
                        pending = (
                            await conn.fetch(
                                "SELECT block_id,topic_id FROM study_blocks WHERE plan_id=$1 AND status='pending'",
                                day["current_plan_id"],
                            )
                            if day["current_plan_id"] is not None
                            else []
                        )
                        completed_after = completed - reopened
                        invalid_topics = {
                            block["topic_id"]
                            for block in pending
                            if block["topic_id"] not in topics
                            or block["topic_id"] in completed_after
                            or not set(topics[block["topic_id"]]["prerequisites"])
                            <= completed_after
                        }
                        if invalid_topics:
                            await conn.execute(
                                "UPDATE study_blocks SET status='cancelled' WHERE plan_id=$1 AND status='pending' AND topic_id=ANY($2::text[])",
                                day["current_plan_id"],
                                list(invalid_topics),
                            )
                        await conn.execute(
                            "UPDATE study_days SET state=CASE WHEN $2 THEN 'replan_required' ELSE state END,queue_revision=queue_revision+1 WHERE study_date=$1",
                            today,
                            bool(invalid_topics),
                        )
                        await conn.execute(
                            "UPDATE engine_state SET state_revision=state_revision+1,progress_revision=progress_revision+1 WHERE singleton=true"
                        )
                        if (
                            not invalid_topics
                            and day["state"] != "replan_required"
                            and day["current_plan_id"] is not None
                        ):
                            await conn.execute(
                                "UPDATE plans SET state_revision=(SELECT state_revision FROM engine_state WHERE singleton=true) WHERE plan_id=$1",
                                day["current_plan_id"],
                            )
                        text, code = (
                            f"Reopened {', '.join(sorted(reopened))}. Recorded study time and history were preserved; use /replan to refresh the queue if needed.",
                            "TOPICS_REOPENED",
                        )
        if confirmation is not None:
            await conn.execute(
                "UPDATE action_confirmations SET consumed_at=now(),result_code=$2,result_text=$3 WHERE token=$1 AND consumed_at IS NULL AND actor_id=$4 AND chat_id=$5 AND recovery_epoch=$6",
                token,
                code,
                text,
                actor_id,
                chat_id,
                state["recovery_epoch"],
            )
    elif mode == "review":
        supplied = arguments[0] if arguments else None
        try:
            study_date = date.fromisoformat(supplied) if supplied else today
            if supplied and study_date.isoformat() != supplied:
                raise ValueError
        except ValueError:
            study_date = None
        if study_date is None or study_date > today:
            text, code = (
                "Use /progress [YYYY-MM-DD] with a date no later than today in Asia/Jakarta.",
                "INVALID_DATE",
            )
        else:
            rows = await conn.fetch(
                "SELECT session_id,topic_id,actual_minutes FROM study_sessions WHERE study_date=$1 ORDER BY session_id",
                study_date,
            )
            total = sum(r["actual_minutes"] for r in rows)
            details = (
                "; ".join(
                    f"{r['session_id']} {r['topic_id']} {r['actual_minutes']}m"
                    for r in rows
                )
                or "no sessions"
            )
            text, code = (
                f"Progress for {study_date}: {total}/1440 minutes; {details}. Add: /progress add TOPIC MINUTES [YYYY-MM-DD].",
                "PROGRESS_HISTORY",
            )
    elif mode == "add" and len(arguments) in (3, 4):
        topic_id, raw_minutes = arguments[1:3]
        supplied = arguments[3] if len(arguments) == 4 else today.isoformat()
        minutes, minutes_error = parse_actual_minutes([raw_minutes])
        try:
            study_date = date.fromisoformat(supplied)
            if study_date.isoformat() != supplied:
                raise ValueError
        except ValueError:
            study_date = None
        known = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM curriculum_versions v, jsonb_array_elements(v.content->'topics') t WHERE t->>'topic_id'=$1)",
            topic_id,
        )
        if study_date is None or study_date > today:
            text, code = (
                "Historical progress requires a valid date no later than today in Asia/Jakarta.",
                "INVALID_DATE",
            )
        elif minutes_error:
            text, code = (
                "Minutes must be a whole number from 0 to 1440.",
                minutes_error,
            )
        elif not known:
            text, code = (
                "Unknown topic. Use a topic identity from the curriculum.",
                "UNKNOWN_TOPIC",
            )
        else:
            total = await conn.fetchval(
                "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                study_date,
            )
            if total + minutes > 1440:
                text, code = (
                    "The daily total would exceed 1440 minutes.",
                    "DAILY_LIMIT_EXCEEDED",
                )
            else:
                session_id = uuid.uuid4()
                queue_revision = await conn.fetchval(
                    "SELECT COALESCE((SELECT queue_revision FROM study_days WHERE study_date=$1),0)",
                    study_date,
                )
                token = await issue_action_confirmation(
                    conn,
                    actor_id,
                    chat_id,
                    "progress_add",
                    session_id,
                    study_date,
                    state["state_revision"],
                    queue_revision,
                    state["recovery_epoch"],
                    context={
                        "session_id": str(session_id),
                        "topic_id": topic_id,
                        "study_date": study_date.isoformat(),
                        "new_date": study_date.isoformat(),
                        "minutes": minutes,
                    },
                )
                text, code = (
                    f"Add {minutes} minutes for {topic_id} on {study_date}? Confirm with /progress confirm {token}.",
                    "PROGRESS_CONFIRMATION_REQUIRED",
                )
    elif mode == "edit" and len(arguments) == 4:
        minutes, minutes_error = parse_actual_minutes([arguments[3]])
        try:
            session_id = uuid.UUID(arguments[1])
            new_date = date.fromisoformat(arguments[2])
            if new_date.isoformat() != arguments[2]:
                raise ValueError
        except ValueError:
            session_id, new_date = None, None
        before = (
            await conn.fetchrow(
                "SELECT * FROM study_sessions WHERE session_id=$1", session_id
            )
            if session_id
            else None
        )
        if new_date is None or new_date > today:
            text, code = (
                "Correction date must be valid and no later than today.",
                "INVALID_DATE",
            )
        elif minutes_error:
            text, code = (
                "Minutes must be a whole number from 0 to 1440.",
                minutes_error,
            )
        elif before is None:
            text, code = (
                "Unknown session. Review /progress for its date.",
                "UNKNOWN_SESSION",
            )
        else:
            total = await conn.fetchval(
                "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                new_date,
            )
            effective = (
                total
                - (before["actual_minutes"] if before["study_date"] == new_date else 0)
                + minutes
            )
            if effective > 1440:
                text, code = (
                    "The corrected daily total would exceed 1440 minutes.",
                    "DAILY_LIMIT_EXCEEDED",
                )
            else:
                queue_revision = await conn.fetchval(
                    "SELECT COALESCE((SELECT queue_revision FROM study_days WHERE study_date=$1),0)",
                    before["study_date"],
                )
                new_queue_revision = await conn.fetchval(
                    "SELECT COALESCE((SELECT queue_revision FROM study_days WHERE study_date=$1),0)",
                    new_date,
                )
                token = await issue_action_confirmation(
                    conn,
                    actor_id,
                    chat_id,
                    "progress_edit",
                    session_id,
                    before["study_date"],
                    state["state_revision"],
                    queue_revision,
                    state["recovery_epoch"],
                    context={
                        "session_id": str(session_id),
                        "topic_id": before["topic_id"],
                        "study_date": before["study_date"].isoformat(),
                        "before_date": before["study_date"].isoformat(),
                        "before_minutes": before["actual_minutes"],
                        "new_date": new_date.isoformat(),
                        "new_queue_revision": new_queue_revision,
                        "minutes": minutes,
                    },
                )
                text, code = (
                    f"Correct session {session_id} ({before['study_date']} {before['actual_minutes']}m) to {new_date} {minutes}m? Confirm with /progress confirm {token}.",
                    "PROGRESS_CONFIRMATION_REQUIRED",
                )
    elif mode == "reopen" and len(arguments) == 2:
        topic_id = arguments[1]
        if not state["active_curriculum_id"]:
            text, code = "There is no active curriculum.", "NO_ACTIVE_CURRICULUM"
        else:
            curriculum = await conn.fetchrow(
                "SELECT content FROM curriculum_versions WHERE curriculum_id=$1 AND status='approved'",
                state["active_curriculum_id"],
            )
            content = (
                curriculum["content"]
                if isinstance(curriculum["content"], dict)
                else json.loads(curriculum["content"])
            )
            topics = {t["topic_id"]: t for t in content["topics"]}
            completed = {
                r["topic_id"]
                for r in await conn.fetch(
                    "SELECT topic_id FROM topic_progress WHERE completed=true"
                )
            }
            if topic_id not in topics or topic_id not in completed:
                text, code = (
                    "Topic is not a completed topic in the active curriculum.",
                    "UNKNOWN_TOPIC",
                )
            else:
                reopened = {topic_id} | completed_dependents(
                    topic_id, topics, completed
                )
                queue_revision = await conn.fetchval(
                    "SELECT COALESCE((SELECT queue_revision FROM study_days WHERE study_date=$1),0)",
                    today,
                )
                token = await issue_action_confirmation(
                    conn,
                    actor_id,
                    chat_id,
                    "progress_reopen",
                    topic_id,
                    today,
                    state["state_revision"],
                    queue_revision,
                    state["recovery_epoch"],
                    context={
                        "topics": sorted(reopened),
                        "study_date": today.isoformat(),
                    },
                )
                text, code = (
                    f"Reopen {topic_id} and completed dependents {', '.join(sorted(reopened - {topic_id})) or 'none'}? Study time is preserved. Confirm with /progress confirm {token}.",
                    "PROGRESS_CONFIRMATION_REQUIRED",
                )
    else:
        text, code = (
            "Use /progress [YYYY-MM-DD], /progress add TOPIC MINUTES [DATE], /progress edit SESSION_ID DATE MINUTES, or /progress reopen TOPIC_ID.",
            "INVALID_ARGUMENTS",
        )
    await conn.execute(
        "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code',$2::text)) WHERE operation_id=$1",
        operation_id,
        code,
    )
    await conn.execute(
        "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'progress',jsonb_build_object('text',$4::text)) ON CONFLICT(logical_key) DO NOTHING",
        uuid.uuid4(),
        f"operation:{operation_id}:progress",
        operation_id,
        text,
    )


async def handle_block_command(
    conn,
    operation_id,
    command: str,
    arguments: list[str],
    actor_id: int,
    chat_id: int,
    token_authorized=False,
):
    from datetime import datetime

    from app.planning import JAKARTA

    today = datetime.now(JAKARTA).date()
    await lock_study_date_requests(conn, today)
    # Match the planner's advisory -> job -> engine-state -> day lock order.
    await conn.fetch(
        "SELECT job_id FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running') FOR UPDATE",
        today,
    )
    await conn.fetchrow(
        "SELECT state_revision FROM engine_state WHERE singleton=true FOR UPDATE"
    )
    await conn.execute(
        "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT(study_date) DO NOTHING",
        today,
    )
    day = await conn.fetchrow(
        "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", today
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
    confirmation_token = None
    if command == "done" and len(arguments) == 2:
        confirmation_token = arguments[0]
        confirmation = await conn.fetchrow(
            "SELECT * FROM action_confirmations WHERE token=$1 FOR UPDATE",
            confirmation_token,
        )
        now = await conn.fetchval("SELECT now()")
        state = await conn.fetchrow("SELECT * FROM engine_state WHERE singleton=true")
        if confirmation is not None and confirmation["consumed_at"] is not None:
            if confirmation["action"] != "block_done":
                text, code = (
                    "Study state changed. Use /done to choose again.",
                    "STALE_ACTION",
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
                return
            if (
                confirmation["actor_id"] != actor_id
                or confirmation["chat_id"] != chat_id
                or confirmation["recovery_epoch"] != state["recovery_epoch"]
            ):
                text, code = (
                    "Study state changed. Use /done to choose again.",
                    "STALE_ACTION",
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
            else:
                text, code = confirmation["result_text"], confirmation["result_code"]
                await conn.execute(
                    "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code',$2::text)) WHERE operation_id=$1",
                    operation_id,
                    code,
                )
            return
        candidate = next(
            (
                b
                for b in blocks
                if confirmation is not None
                and str(b["block_id"]) == confirmation["target"]
            ),
            None,
        )
        if (
            confirmation is None
            or confirmation["action"] != "block_done"
            or candidate is None
            or confirmation["actor_id"] != actor_id
            or confirmation["chat_id"] != chat_id
            or confirmation["recovery_epoch"] != state["recovery_epoch"]
            or confirmation["study_date"] != today
            or confirmation["expires_at"] <= now
            or confirmation["state_revision"] != state["state_revision"]
            or confirmation["queue_revision"] != day["queue_revision"]
        ):
            text, code = (
                "Study state changed. Use /done to choose again.",
                "STALE_ACTION",
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
            return
        arguments = [
            str(candidate["block_id"]),
            str(day["queue_revision"]),
            arguments[1],
        ]
        token_authorized = True
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
        state = await conn.fetchrow("SELECT * FROM engine_state WHERE singleton=true")
        token = await issue_action_confirmation(
            conn,
            actor_id,
            chat_id,
            "block_done",
            target["block_id"],
            today,
            state["state_revision"],
            day["queue_revision"],
            state["recovery_epoch"],
            target["planned_minutes"],
        )
        text = f"Confirm {target['topic_id']} with {target['planned_minutes']} minutes, or send /done {token} {target['planned_minutes']} (0–1440)."
        code = "DONE_CONFIRMATION_REQUIRED"
    elif command == "done" and len(arguments) == 3 and not token_authorized:
        text, code = (
            "Use the tokenized confirmation shown in the preview.",
            "INVALID_ARGUMENTS",
        )
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
                if code == "BLOCK_DONE" and day["current_plan_id"] is not None:
                    await conn.execute(
                        "UPDATE plans SET state_revision=(SELECT state_revision FROM engine_state WHERE singleton=true) WHERE plan_id=$1",
                        day["current_plan_id"],
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
    if confirmation_token is not None and code in ("BLOCK_DONE", "REPLAN_REQUIRED"):
        await conn.execute(
            "UPDATE action_confirmations SET consumed_at=now(),result_code=$2,result_text=$3 WHERE token=$1",
            confirmation_token,
            code,
            text,
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
            "complete_topic",
            "progress",
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
        elif status == "accepted" and body.payload.command == "complete_topic":
            await handle_topic_command(
                conn, operation_id, body.payload.arguments, actor_id, chat_id
            )
        elif status == "accepted" and body.payload.command == "progress":
            await handle_progress_command(
                conn, operation_id, body.payload.arguments, actor_id, chat_id
            )
        elif status == "accepted" and body.payload.command in ("done", "skip"):
            await handle_block_command(
                conn,
                operation_id,
                body.payload.command,
                body.payload.arguments,
                actor_id,
                chat_id,
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
            # Keep all waiting operations attached to the newest generation. Locks
            # follow the worker's job -> engine-state -> day order, so a worker either
            # commits before this request or observes that its job was retired.
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
                    "INSERT INTO deliveries(delivery_id,logical_key,operation_id,study_date,kind,payload) VALUES($1,$2,$3,$4,'today',jsonb_build_object('text',$5::text)) ON CONFLICT(logical_key) DO NOTHING",
                    uuid.uuid4(),
                    f"operation:{operation_id}:today",
                    operation_id,
                    study_date,
                    "Today's queue needs to be refreshed. Use /replan to create a replacement queue.",
                )
            else:
                await conn.execute(
                    "UPDATE study_days SET routine_suppressed=true WHERE study_date=$1",
                    study_date,
                )
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
                            "INSERT INTO deliveries(delivery_id,logical_key,operation_id,study_date,kind,payload) VALUES($1,$2,$3,$4,'today',jsonb_build_object('text',$5::text)) ON CONFLICT(logical_key) DO NOTHING",
                            uuid.uuid4(),
                            f"operation:{operation_id}:today",
                            operation_id,
                            study_date,
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
        stored_result = await conn.fetchval(
            "SELECT result FROM operations WHERE operation_id=$1", operation_id
        )
        return (
            json.loads(stored_result)
            if isinstance(stored_result, str)
            else stored_result
        )


@app.post("/v1/deliveries/claim", dependencies=[Depends(service_auth)])
async def claim_delivery():
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            """UPDATE deliveries SET status='failed',lease_until=NULL
               WHERE status IN ('pending','leased') AND (
                   expires_at <= now() OR
                   (kind IN ('today','routine') AND study_date < (now() AT TIME ZONE 'Asia/Jakarta')::date)
               )"""
        )
        row = await conn.fetchrow(
            """SELECT d.delivery_id,d.operation_id,d.payload,d.fence FROM deliveries d
               WHERE d.expires_at > now()
                 AND (d.study_date IS NULL OR d.kind NOT IN ('today','routine')
                      OR d.study_date >= (now() AT TIME ZONE 'Asia/Jakarta')::date)
                 AND (d.status='pending' AND d.next_attempt_at <= now()
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
