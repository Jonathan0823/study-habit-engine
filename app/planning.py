"""Deterministic, model-free daily queue allocation and durable planning worker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.calendar import (
    GoogleCalendarClient,
    persist_calendar_reminder,
    sync_calendar_reminders,
)
from langgraph.graph import END, START, StateGraph

from app.ranking import (
    configured_gemini_ranker,
    max_input_token_limit,
    ranking_input_fits,
    validate_ranking,
)

JAKARTA = ZoneInfo("Asia/Jakarta")
logger = logging.getLogger(__name__)


class Candidate(TypedDict):
    topic_id: str
    position: int
    estimate_minutes: int
    prerequisites: list[str]
    completed_prerequisites: list[str]
    started_minutes: int
    title: str
    description: str


def allocate(
    candidates: list[Candidate],
    capacity: int,
    skipped_topic_ids: set[str] | None = None,
    ranking_order: list[str] | None = None,
) -> list[dict]:
    """Allocate 30-minute blocks in repeated passes; never create work or filler."""
    capacity = max(0, min(90, capacity))
    skipped_topic_ids = skipped_topic_ids or set()
    eligible = [
        item
        for item in candidates
        if item["topic_id"] not in skipped_topic_ids
        and set(item["prerequisites"]) <= set(item["completed_prerequisites"])
    ]
    rank_positions = (
        {topic_id: index for index, topic_id in enumerate(ranking_order)}
        if ranking_order is not None
        else {}
    )
    eligible.sort(
        key=lambda item: (
            item["started_minutes"] <= 0,
            rank_positions.get(item["topic_id"], item["position"]),
            item["position"],
            item["topic_id"],
        )
    )
    if capacity < 15 or not eligible:
        return []
    blocks: list[dict] = []
    while capacity >= 15:
        allocated = False
        for topic in eligible:
            if capacity < 15:
                break
            minutes = min(30, capacity)
            blocks.append({"topic_id": topic["topic_id"], "planned_minutes": minutes})
            capacity -= minutes
            allocated = True
        if not allocated:
            break
    return blocks


class GraphState(TypedDict, total=False):
    job_id: str
    result: dict


def build_graph(checkpointer, pool: asyncpg.Pool, owner: uuid.UUID):
    async def snapshot_allocate_commit(state: GraphState) -> GraphState:
        # The authoritative snapshot, deterministic allocation, and plan/outbox
        # commit happen together; checkpoint state is never treated as business truth.
        await process_job(pool, uuid.UUID(state["job_id"]), owner)
        return {"result": {"job_id": state["job_id"], "committed": True}}

    graph = StateGraph(GraphState)
    graph.add_node("snapshot_allocate_commit", snapshot_allocate_commit)
    graph.add_edge(START, "snapshot_allocate_commit")
    graph.add_edge("snapshot_allocate_commit", END)
    return graph.compile(checkpointer=checkpointer)


async def process_job(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    owner: uuid.UUID,
    *,
    ranker=None,
) -> None:
    """Claim once, optionally rank outside transactions, then revalidate and commit."""
    if ranker is None:
        try:
            ranker = configured_gemini_ranker()
        except Exception:
            ranker = None
    prepared = await _claim_and_reserve_ai(pool, job_id, owner, ranker)
    if prepared is None:
        return
    fence, candidates, saved_ranking, dispatch = prepared
    ranking_response = saved_ranking
    if dispatch and ranker is not None:
        try:
            response = await asyncio.wait_for(ranker(candidates), timeout=10)
            valid = validate_ranking(candidates, response)
            if valid is None:
                await _finish_ai_attempt(pool, job_id, None, "INVALID_RANKING", uncertain=False)
            else:
                ranking_response = _canonical_ranking_response(response)
                await _finish_ai_attempt(pool, job_id, ranking_response, None, uncertain=False)
        except Exception:
            await _finish_ai_attempt(pool, job_id, None, "PROVIDER_UNAVAILABLE", uncertain=True)
    await _commit_claimed_job(pool, job_id, owner, fence, ranking_response)


def _canonical_ranking_response(response):
    if hasattr(response, "model_dump"):
        response = response.model_dump(exclude_none=True)
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return None
    return response if isinstance(response, dict) else None


async def _claim_and_reserve_ai(pool, job_id, owner, ranker):
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            """SELECT * FROM planning_jobs WHERE job_id=$1 AND
                   (status='pending' OR status='running' AND lease_until < now()) FOR UPDATE""",
            job_id,
        )
        if job is None:
            return None
        fence = job["fence"] + 1
        await conn.execute(
            "UPDATE planning_jobs SET status='running',lease_owner=$2,fence=$3,lease_until=now()+interval '60 seconds' WHERE job_id=$1",
            job_id,
            owner,
            fence,
        )
        today = job["study_date"]
        state = await conn.fetchrow(
            "SELECT active_curriculum_id FROM engine_state WHERE singleton=true FOR UPDATE"
        )
        day = await conn.fetchrow(
            "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", today
        )
        if (
            job["generation"] != day["requested_generation"]
            or today != datetime.now(JAKARTA).date()
            or state is None
            or state["active_curriculum_id"] is None
        ):
            return fence, [], None, False
        curriculum = await conn.fetchrow(
            "SELECT status,content FROM curriculum_versions WHERE curriculum_id=$1",
            state["active_curriculum_id"],
        )
        if curriculum is None or curriculum["status"] != "approved":
            return fence, [], None, False
        content = curriculum["content"]
        if isinstance(content, str):
            content = json.loads(content)
        actual, completed, skipped, candidates = await _candidate_snapshot(
            conn, today, content
        )
        eligible = [
            candidate
            for candidate in candidates
            if candidate["topic_id"] not in skipped
            and set(candidate["prerequisites"])
            <= set(candidate["completed_prerequisites"])
        ]
        input_limit = max_input_token_limit()
        if (
            not eligible
            or day["budget_minutes"] - actual < 15
            or input_limit is None
            or not ranking_input_fits(eligible, input_limit)
        ):
            return fence, eligible, None, False

        attempt = await conn.fetchrow(
            "SELECT status,ranking FROM ai_attempts WHERE job_id=$1 FOR UPDATE", job_id
        )
        if attempt is not None:
            ranking = attempt["ranking"]
            if isinstance(ranking, str):
                ranking = json.loads(ranking)
            return fence, eligible, ranking if attempt["status"] == "finished" else None, False
        if ranker is None:
            return fence, eligible, None, False

        # Locking engine_state serializes daily and monthly reservations. Only
        # free-tier calls have a nonpositive price; paid spillover is never allowed.
        limits = await conn.fetchrow(
            """SELECT count(*)::int AS calls, coalesce(sum(reserved_usd),0) AS reserved
               FROM ai_attempts WHERE study_date=$1""",
            today,
        )
        month_start = date(today.year, today.month, 1)
        monthly_reserved = await conn.fetchval(
            "SELECT coalesce(sum(reserved_usd),0) FROM ai_attempts WHERE month_start=$1",
            month_start,
        )
        if limits["calls"] >= 10 or Decimal(monthly_reserved) > Decimal("0"):
            return fence, eligible, None, False

        await conn.execute(
            """INSERT INTO ai_attempts(
                   job_id,study_date,month_start,provider,model,pricing_revision,
                   reserved_usd,status)
               VALUES($1,$2,$3,'google_gemini',$4,'gemini-free-tier-v1',0,'dispatched')""",
            job_id,
            today,
            month_start,
            os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        )
        return fence, eligible, None, True


async def _candidate_snapshot(conn, today, content):
    sessions = await conn.fetch(
        "SELECT topic_id,sum(actual_minutes)::int AS minutes FROM study_sessions WHERE study_date=$1 GROUP BY topic_id",
        today,
    )
    actual = sum(row["minutes"] for row in sessions)
    history = await conn.fetch(
        "SELECT topic_id,sum(actual_minutes)::int AS minutes FROM study_sessions GROUP BY topic_id"
    )
    started = {row["topic_id"]: row["minutes"] for row in history if row["minutes"] > 0}
    progress_minutes = await conn.fetch(
        "SELECT topic_id,study_minutes FROM topic_progress WHERE study_minutes > 0"
    )
    started.update(
        {
            row["topic_id"]: max(started.get(row["topic_id"], 0), row["study_minutes"])
            for row in progress_minutes
        }
    )
    completed_rows = await conn.fetch(
        "SELECT topic_id FROM topic_progress WHERE completed=true"
    )
    completed = {row["topic_id"] for row in completed_rows}
    skipped_rows = await conn.fetch(
        "SELECT DISTINCT b.topic_id FROM study_blocks b JOIN plans p USING(plan_id) WHERE p.study_date=$1 AND b.status='skipped'",
        today,
    )
    skipped = {row["topic_id"] for row in skipped_rows}
    candidates = [
        {
            "topic_id": topic["topic_id"],
            "title": topic["title"],
            "description": topic["description"],
            "position": topic["position"],
            "estimate_minutes": topic["estimate_minutes"],
            "prerequisites": topic["prerequisites"],
            "completed_prerequisites": sorted(completed),
            "started_minutes": started.get(topic["topic_id"], 0),
        }
        for topic in content["topics"]
        if topic["topic_id"] not in completed
    ]
    return actual, completed, skipped, candidates


async def _finish_ai_attempt(pool, job_id, ranking, error_code, *, uncertain):
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """UPDATE ai_attempts SET status=$2,ranking=$3::jsonb,error_code=$4,
                   actual_usd=0,finished_at=now()
               WHERE job_id=$1 AND status='dispatched'""",
            job_id,
            "uncertain" if uncertain else "finished",
            json.dumps(ranking) if ranking is not None else None,
            error_code,
        )


async def _commit_claimed_job(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    owner: uuid.UUID,
    fence: int,
    ranking_response: dict | None,
) -> None:
    """Re-snapshot and atomically commit only for the current lease owner."""
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            "SELECT * FROM planning_jobs WHERE job_id=$1 AND status='running' AND lease_owner=$2 AND fence=$3 AND lease_until > now() FOR UPDATE",
            job_id,
            owner,
            fence,
        )
        if job is None:
            return
        today = job["study_date"]
        state = await conn.fetchrow(
            "SELECT active_curriculum_id,state_revision FROM engine_state WHERE singleton=true FOR UPDATE"
        )
        day = await conn.fetchrow(
            "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", today
        )
        if today != datetime.now(JAKARTA).date():
            await _expire_old_job(conn, job, day)
            return
        if job["generation"] != day["requested_generation"]:
            reason = "PLAN_SUPERSEDED"
            text = "A newer planning request replaced this one; use /today to see the current queue."
            operation_ids = await _job_operations(conn, job)
            for operation_id in operation_ids:
                await conn.execute(
                    "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code',$2::text,'reason',$2::text)) WHERE operation_id=$1",
                    operation_id,
                    reason,
                )
            for operation_id in operation_ids:
                await _deliver(conn, operation_id, text, today)
            await conn.execute(
                "UPDATE planning_jobs SET status='completed',result=$2::jsonb,lease_until=NULL WHERE job_id=$1",
                job_id,
                json.dumps({"reason": "PLAN_SUPERSEDED"}),
            )
            return
        if state is None or state["active_curriculum_id"] is None:
            await _no_plan(
                conn,
                job,
                "NO_APPROVED_CURRICULUM"
                if state is None or state["active_curriculum_id"] is None
                else "PLAN_EXPIRED",
                day,
            )
            return
        curriculum = await conn.fetchrow(
            "SELECT status,content_hash,content FROM curriculum_versions WHERE curriculum_id=$1",
            state["active_curriculum_id"],
        )
        if curriculum is None or curriculum["status"] != "approved":
            await _no_plan(conn, job, "NO_APPROVED_CURRICULUM", day)
            return
        content = curriculum["content"]
        if isinstance(content, str):
            content = json.loads(content)
        actual, completed, skipped_topic_ids, candidates = await _candidate_snapshot(
            conn, today, content
        )
        rankable = [
            candidate
            for candidate in candidates
            if candidate["topic_id"] not in skipped_topic_ids
            and set(candidate["prerequisites"])
            <= set(candidate["completed_prerequisites"])
        ]
        ranking_order = (
            validate_ranking(rankable, ranking_response)
            if ranking_response is not None
            else None
        )
        blocks = allocate(
            candidates,
            min(day["budget_minutes"], 90) - actual,
            skipped_topic_ids,
            ranking_order,
        )
        if not blocks:
            eligible = any(
                t["topic_id"] not in completed
                and t["topic_id"] not in skipped_topic_ids
                and set(t["prerequisites"]) <= completed
                for t in content["topics"]
            )
            reason = (
                "REST_DAY"
                if day["budget_minutes"] == 0
                else "NO_BLOCK_FITS"
                if eligible
                else "NO_ELIGIBLE_TOPICS"
            )
            await _no_plan(conn, job, reason, day)
            return
        plan_id = uuid.uuid4()
        queue_rev = day["queue_revision"] + 1
        await conn.execute(
            "INSERT INTO plans(plan_id,study_date,curriculum_id,generation,state_revision,queue_revision) VALUES($1,$2,$3,$4,$5,$6)",
            plan_id,
            today,
            state["active_curriculum_id"],
            job["generation"],
            state["state_revision"],
            queue_rev,
        )
        for pos, block in enumerate(blocks):
            await conn.execute(
                "INSERT INTO study_blocks(block_id,plan_id,position,topic_id,planned_minutes) VALUES($1,$2,$3,$4,$5)",
                uuid.uuid4(),
                plan_id,
                pos,
                block["topic_id"],
                block["planned_minutes"],
            )
        await persist_calendar_reminder(conn, today, plan_id, content, blocks)
        lines = render_queue(
            blocks, content, completed, min(day["budget_minutes"], 90) - actual
        )
        previous_plan_id = day["current_plan_id"]
        if previous_plan_id is not None:
            await conn.execute(
                "UPDATE study_blocks SET status='cancelled' WHERE plan_id=$1 AND status='pending'",
                previous_plan_id,
            )
        await conn.execute(
            "UPDATE study_days SET current_plan_id=$2,queue_revision=$3,state='valid' WHERE study_date=$1",
            today,
            plan_id,
            queue_rev,
        )
        operation_ids = await _job_operations(conn, job)
        for operation_id in operation_ids:
            await conn.execute(
                "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code','PLAN_READY','plan_id',$2::text)) WHERE operation_id=$1",
                operation_id,
                str(plan_id),
            )
        for operation_id in operation_ids:
            await _deliver(conn, operation_id, "\n".join(lines), today)
        await conn.execute(
            "UPDATE planning_jobs SET status='completed',result=$2::jsonb,lease_until=NULL WHERE job_id=$1 AND fence=$3",
            job_id,
            json.dumps({"plan_id": str(plan_id)}),
            fence,
        )


async def _job_operations(conn, job) -> list[uuid.UUID]:
    rows = await conn.fetch(
        "SELECT operation_id FROM planning_job_operations WHERE job_id=$1 UNION SELECT $2::uuid",
        job["job_id"],
        job["operation_id"],
    )
    return [row["operation_id"] for row in rows]


async def _deliver(
    conn, operation_id: uuid.UUID, text: str, study_date: date | None = None
) -> None:
    source_key = await conn.fetchval(
        "SELECT source_key FROM operations WHERE operation_id=$1", operation_id
    )
    if source_key.startswith("routine:"):
        study_date = date.fromisoformat(source_key.removeprefix("routine:"))
        suppressed = await conn.fetchval(
            "SELECT routine_suppressed FROM study_days WHERE study_date=$1",
            study_date,
        )
        if suppressed:
            return
        logical_key = f"routine:{study_date.isoformat()}:telegram"
        kind = "routine"
    else:
        logical_key = f"operation:{operation_id}:today"
        kind = "today"
    await conn.execute(
        """INSERT INTO deliveries(delivery_id,logical_key,operation_id,study_date,kind,payload,expires_at)
           VALUES($1,$2,$3,$4,$5,jsonb_build_object('text',$6::text),
             CASE WHEN $5='routine' THEN (($4::date + 1)::timestamp AT TIME ZONE 'Asia/Jakarta')
                  ELSE now()+interval '1 hour' END)
           ON CONFLICT(logical_key) DO NOTHING""",
        uuid.uuid4(),
        logical_key,
        operation_id,
        study_date,
        kind,
        text,
    )


async def _expire_old_job(conn, job, day) -> None:
    """Expire a delayed prior-date job without creating a catch-up delivery."""
    study_date = job["study_date"]
    await conn.execute(
        """UPDATE study_blocks b SET status='cancelled' FROM plans p
           WHERE b.plan_id=p.plan_id AND p.study_date=$1 AND b.status='pending'""",
        study_date,
    )
    await conn.execute(
        """UPDATE study_days SET current_plan_id=NULL,state='absent',
               queue_revision=queue_revision+1 WHERE study_date=$1""",
        study_date,
    )
    await conn.execute(
        """UPDATE calendar_reminders SET state=CASE WHEN ever_synced THEN 'synced'
                   ELSE 'cancelled' END,next_check_at=NULL,next_retry_at=NULL,updated_at=now()
           WHERE study_date=$1 AND state NOT IN ('suppressed','auth_required')""",
        study_date,
    )
    operation_ids = await _job_operations(conn, job)
    for operation_id in operation_ids:
        await conn.execute(
            """UPDATE operations SET status='completed',
                   result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),
                                    '{result}',jsonb_build_object('code','PLAN_EXPIRED'))
               WHERE operation_id=$1""",
            operation_id,
        )
    await conn.execute(
        "UPDATE planning_jobs SET status='completed',result=$2::jsonb,lease_until=NULL WHERE job_id=$1",
        job["job_id"],
        json.dumps({"reason": "PLAN_EXPIRED"}),
    )
    await conn.execute(
        """UPDATE deliveries SET status='failed',lease_until=NULL
           WHERE kind IN ('today','routine') AND study_date=$1
             AND status IN ('pending','leased')""",
        study_date,
    )


async def _no_plan(conn, job, reason: str, day) -> None:
    previous_plan_id = day["current_plan_id"]
    if previous_plan_id is not None:
        await conn.execute(
            "UPDATE study_blocks SET status='cancelled' WHERE plan_id=$1 AND status='pending'",
            previous_plan_id,
        )
    await conn.execute(
        "UPDATE study_days SET current_plan_id=NULL,queue_revision=$2,state='absent' WHERE study_date=$1",
        day["study_date"],
        day["queue_revision"] + 1,
    )
    text = {
        "NO_APPROVED_CURRICULUM": "No approved curriculum is available, so I can't make a queue.",
        "PLAN_EXPIRED": "That request expired; request /today for the current day.",
        "NO_ELIGIBLE_TOPICS": "No eligible study topics are available right now.",
        "REST_DAY": "Today's budget is 0 minutes, so this is a rest day.",
        "NO_BLOCK_FITS": "Eligible topics exist, but no study block fits the available time (minimum 15 minutes).",
    }.get(reason, "No study block fits the available time (minimum 15 minutes).")
    operation_ids = await _job_operations(conn, job)
    for operation_id in operation_ids:
        await conn.execute(
            "UPDATE operations SET status='completed',result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),'{result}',jsonb_build_object('code',$2::text)) WHERE operation_id=$1",
            operation_id,
            reason,
        )
    for operation_id in operation_ids:
        await _deliver(conn, operation_id, text, job["study_date"])
    await conn.execute(
        "UPDATE planning_jobs SET status='completed',result=$2::jsonb,lease_until=NULL WHERE job_id=$1",
        job["job_id"],
        json.dumps({"reason": reason}),
    )


def render_queue(
    blocks: list[dict], content: dict, completed: set[str], capacity: int
) -> list[str] | None:
    """Validate a queue against current approved content, then render stored resources."""
    if not blocks or sum(block["planned_minutes"] for block in blocks) > max(
        0, capacity
    ):
        return None
    topics = {topic["topic_id"]: topic for topic in content["topics"]}
    lines = ["Today's study queue:"]
    for index, block in enumerate(blocks, 1):
        topic = topics.get(block["topic_id"])
        minutes = block["planned_minutes"]
        if (
            topic is None
            or topic["topic_id"] in completed
            or not set(topic["prerequisites"]) <= completed
            or not 15 <= minutes <= 45
        ):
            return None
        lines.append(f"{index}. {topic['title']} — {minutes} min")
        lines.extend(
            f"{resource['title']}: {resource['url']}" for resource in topic["resources"]
        )
    return lines


async def next_job(pool: asyncpg.Pool):
    """Find a job the worker can claim, including an expired worker's lease."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT job_id FROM planning_jobs WHERE status='pending' OR (status='running' AND lease_until < now()) ORDER BY created_at LIMIT 1"
        )


async def _current_queue_for_routine(conn, study_date: date, day) -> str | None:
    plan = await conn.fetchrow(
        """SELECT v.content FROM plans p
           JOIN curriculum_versions v ON v.curriculum_id=p.curriculum_id
           JOIN engine_state s ON s.singleton=true AND s.active_curriculum_id=p.curriculum_id
             AND s.state_revision=p.state_revision
           WHERE p.plan_id=$1 AND p.study_date=$2 AND v.status='approved'""",
        day["current_plan_id"],
        study_date,
    )
    if plan is None:
        return None
    content = plan["content"]
    if isinstance(content, str):
        content = json.loads(content)
    blocks = await conn.fetch(
        "SELECT topic_id,planned_minutes FROM study_blocks WHERE plan_id=$1 AND status='pending' ORDER BY position",
        day["current_plan_id"],
    )
    if not blocks:
        return ""
    await persist_calendar_reminder(
        conn, study_date, day["current_plan_id"], content, blocks
    )
    completed = {
        row["topic_id"]
        for row in await conn.fetch("SELECT topic_id FROM topic_progress WHERE completed=true")
    }
    actual = await conn.fetchval(
        "SELECT COALESCE(sum(actual_minutes),0)::int FROM study_sessions WHERE study_date=$1",
        study_date,
    )
    lines = render_queue(
        blocks, content, completed, min(day["budget_minutes"], 90) - actual
    )
    return "\n".join(lines) if lines else None


async def run_routine_tick(pool: asyncpg.Pool, now: datetime | None = None) -> bool:
    """Durably deliver today's plan once its Jakarta routine time arrives."""
    now = now or datetime.now(JAKARTA)
    if now.tzinfo is None:
        raise ValueError("routine tick requires a timezone-aware datetime")
    local_now = now.astimezone(JAKARTA)
    if (local_now.hour, local_now.minute) < (17, 0):
        return False

    study_date = local_now.date()
    source_key = f"routine:{study_date.isoformat()}"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(0,$1::date - DATE '2000-01-01')",
            study_date,
        )
        await conn.fetch("SELECT job_id FROM planning_jobs WHERE study_date=$1 AND status IN ('pending','running') FOR UPDATE", study_date)
        await conn.fetchrow("SELECT singleton FROM engine_state WHERE singleton=true FOR UPDATE")
        await conn.execute(
            "INSERT INTO study_days(study_date) VALUES($1) ON CONFLICT DO NOTHING",
            study_date,
        )
        day = await conn.fetchrow(
            "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", study_date
        )
        existing = await conn.fetchval(
            "SELECT operation_id FROM operations WHERE source_key=$1", source_key
        )
        if existing is not None:
            return True

        active_job_id = await conn.fetchval(
            """SELECT job_id FROM planning_jobs WHERE study_date=$1
               AND status IN ('pending','running') FOR UPDATE""",
            study_date,
        )
        operation_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO operations(operation_id,source_key,request_hash,status,result)
               VALUES($1,$2,$3,'accepted',$4::jsonb)""",
            operation_id,
            source_key,
            source_key,
            json.dumps({"status": "accepted", "kind": "routine"}),
        )
        if day["budget_minutes"] == 0:
            await conn.execute(
                "UPDATE operations SET status='completed',result=$2::jsonb WHERE operation_id=$1",
                operation_id,
                json.dumps({"status": "completed", "kind": "routine", "result": {"code": "REST_DAY"}}),
            )
            return True

        if day["state"] == "valid" and active_job_id is None:
            text = await _current_queue_for_routine(conn, study_date, day)
            if text == "":
                await conn.execute(
                    "UPDATE operations SET status='completed',result=$2::jsonb WHERE operation_id=$1",
                    operation_id,
                    json.dumps({"status": "completed", "kind": "routine", "result": {"code": "NO_PENDING_BLOCKS"}}),
                )
                return True
            if text is not None:
                await conn.execute(
                    "UPDATE operations SET status='completed',result=$2::jsonb WHERE operation_id=$1",
                    operation_id,
                    json.dumps({"status": "completed", "kind": "routine", "result": {"code": "PLAN_READY", "plan_id": str(day['current_plan_id'])}}),
                )
                await _deliver(conn, operation_id, text, study_date)
                return True
            await conn.execute(
                "UPDATE study_days SET state='replan_required' WHERE study_date=$1",
                study_date,
            )

        if active_job_id is None:
            generation = day["requested_generation"] + 1
            await conn.execute(
                "UPDATE study_days SET requested_generation=$2 WHERE study_date=$1",
                study_date,
                generation,
            )
            active_job_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO planning_jobs(job_id,operation_id,study_date,generation)
                   VALUES($1,$2,$3,$4)""",
                active_job_id,
                operation_id,
                study_date,
                generation,
            )
        else:
            await conn.execute(
                """INSERT INTO planning_job_operations(operation_id,job_id)
                   VALUES($1,$2)""",
                operation_id,
                active_job_id,
            )
        return True


async def run_worker() -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    owner = uuid.uuid4()
    checkpoint_conn = await __import__("psycopg").AsyncConnection.connect(
        os.environ["DATABASE_URL"], autocommit=True
    )
    calendar_client = None
    try:
        # Checkpoint tables are provisioned by app.migrate using migration credentials.
        saver = AsyncPostgresSaver(checkpoint_conn)
        graph = build_graph(saver, pool, owner)
        routine_checked_date = None
        calendar_path = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS_FILE")
        calendar_checked_minute = None
        while True:
            now = datetime.now(JAKARTA)
            if routine_checked_date != now.date() and await run_routine_tick(pool, now):
                routine_checked_date = now.date()
            if calendar_path and Path(calendar_path).is_file():
                if calendar_client is None:
                    calendar_client = GoogleCalendarClient(calendar_path)
                current_minute = now.replace(second=0, microsecond=0)
                if current_minute != calendar_checked_minute:
                    try:
                        await sync_calendar_reminders(pool, calendar_client, now)
                    except Exception as exc:
                        logger.warning("Calendar sync tick failed (%s)", type(exc).__name__)
                    calendar_checked_minute = current_minute
            elif calendar_client is not None:
                await calendar_client.close()
                calendar_client = None
            job_id = await next_job(pool)
            if job_id is None:
                await asyncio.sleep(1)
                continue
            await graph.ainvoke(
                {"job_id": str(job_id)}, {"configurable": {"thread_id": str(job_id)}}
            )
            calendar_checked_minute = None
    finally:
        if calendar_client is not None:
            await calendar_client.close()
        await checkpoint_conn.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
