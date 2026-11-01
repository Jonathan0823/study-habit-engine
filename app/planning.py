"""Deterministic, model-free daily queue allocation and durable planning worker."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import TypedDict
from zoneinfo import ZoneInfo

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

JAKARTA = ZoneInfo("Asia/Jakarta")


class Candidate(TypedDict):
    topic_id: str
    position: int
    estimate_minutes: int
    prerequisites: list[str]
    completed_prerequisites: list[str]
    started_minutes: int


def allocate(
    candidates: list[Candidate],
    capacity: int,
    skipped_topic_ids: set[str] | None = None,
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
    eligible.sort(
        key=lambda item: (
            item["started_minutes"] <= 0,
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


async def process_job(pool: asyncpg.Pool, job_id: uuid.UUID, owner: uuid.UUID) -> None:
    """Claim, snapshot, allocate, and atomically commit plan plus delivery."""
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            """SELECT * FROM planning_jobs WHERE job_id=$1 AND
                   (status='pending' OR status='running' AND lease_until < now()) FOR UPDATE""",
            job_id,
        )
        if job is None:
            return
        fence = job["fence"] + 1
        await conn.execute(
            "UPDATE planning_jobs SET status='running',lease_owner=$2,fence=$3,lease_until=now()+interval '60 seconds' WHERE job_id=$1",
            job_id,
            owner,
            fence,
        )
        today = job["study_date"]
        day = await conn.fetchrow(
            "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE", today
        )
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
                await _deliver(conn, operation_id, text)
            await conn.execute(
                "UPDATE planning_jobs SET status='completed',result=$2::jsonb,lease_until=NULL WHERE job_id=$1",
                job_id,
                json.dumps({"reason": "PLAN_SUPERSEDED"}),
            )
            return
        # Obsolete jobs exit without contending on global engine state.
        state = await conn.fetchrow(
            "SELECT active_curriculum_id,state_revision FROM engine_state WHERE singleton=true FOR UPDATE"
        )
        if (
            today != datetime.now(JAKARTA).date()
            or state is None
            or state["active_curriculum_id"] is None
        ):
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
        sessions = await conn.fetch(
            "SELECT topic_id,sum(actual_minutes)::int AS minutes FROM study_sessions WHERE study_date=$1 GROUP BY topic_id",
            today,
        )
        actual = sum(r["minutes"] for r in sessions)
        history = await conn.fetch(
            "SELECT topic_id,sum(actual_minutes)::int AS minutes FROM study_sessions GROUP BY topic_id"
        )
        started = {r["topic_id"]: r["minutes"] for r in history if r["minutes"] > 0}
        progress_minutes = await conn.fetch(
            "SELECT topic_id,study_minutes FROM topic_progress WHERE study_minutes > 0"
        )
        started.update(
            {
                r["topic_id"]: max(started.get(r["topic_id"], 0), r["study_minutes"])
                for r in progress_minutes
            }
        )
        completed_rows = await conn.fetch(
            "SELECT topic_id FROM topic_progress WHERE completed=true"
        )
        completed = {r["topic_id"] for r in completed_rows}
        skipped_rows = await conn.fetch(
            "SELECT DISTINCT b.topic_id FROM study_blocks b JOIN plans p USING(plan_id) WHERE p.study_date=$1 AND b.status='skipped'",
            today,
        )
        skipped_topic_ids = {r["topic_id"] for r in skipped_rows}
        candidates = [
            {
                "topic_id": t["topic_id"],
                "position": t["position"],
                "estimate_minutes": t["estimate_minutes"],
                "prerequisites": t["prerequisites"],
                "completed_prerequisites": list(completed),
                "started_minutes": started.get(t["topic_id"], 0),
            }
            for t in content["topics"]
            if t["topic_id"] not in completed
        ]
        blocks = allocate(
            candidates,
            min(day["budget_minutes"], 90) - actual,
            skipped_topic_ids,
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
            await _deliver(conn, operation_id, "\n".join(lines))
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


async def _deliver(conn, operation_id: uuid.UUID, text: str) -> None:
    await conn.execute(
        "INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload) VALUES($1,$2,$3,'today',jsonb_build_object('text',$4::text)) ON CONFLICT(logical_key) DO NOTHING",
        uuid.uuid4(),
        f"operation:{operation_id}:today",
        operation_id,
        text,
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
        await _deliver(conn, operation_id, text)
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


async def run_worker() -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    owner = uuid.uuid4()
    checkpoint_conn = await __import__("psycopg").AsyncConnection.connect(
        os.environ["DATABASE_URL"], autocommit=True
    )
    try:
        # Checkpoint tables are provisioned by app.migrate using migration credentials.
        saver = AsyncPostgresSaver(checkpoint_conn)
        graph = build_graph(saver, pool, owner)
        while True:
            job_id = await next_job(pool)
            if job_id is None:
                await asyncio.sleep(1)
                continue
            await graph.ainvoke(
                {"job_id": str(job_id)}, {"configurable": {"thread_id": str(job_id)}}
            )
    finally:
        await checkpoint_conn.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
