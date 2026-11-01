import asyncio
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.db import apply_schema
from app.main import app
from app.planning import process_job


@pytest.fixture
def client():
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL integration test requires MIGRATION_DATABASE_URL")
    os.environ["BOT_SERVICE_TOKEN"] = "bot-secret"
    os.environ["TELEGRAM_BOT_ID"] = "12345"
    os.environ["TELEGRAM_USER_ID"] = "111"
    os.environ["TELEGRAM_PRIVATE_CHAT_ID"] = "111"

    async def setup():
        await apply_schema(url)
        conn = await asyncpg.connect(url)
        try:
            await conn.execute(
                "TRUNCATE planning_jobs, plans, study_days, study_sessions, topic_progress, deliveries, telegram_ingestion, operations, telegram_cursor, curriculum_versions, engine_state CASCADE"
            )
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
            topics = [
                {
                    "topic_id": topic_id,
                    "title": topic_id,
                    "description": f"Learn {topic_id}",
                    "estimate_minutes": 90,
                    "position": position,
                    "prerequisites": [],
                    "resources": [],
                }
                for position, topic_id in enumerate(("a", "b", "c"))
            ]
            curriculum_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO curriculum_versions(curriculum_id,status,content_hash,content) VALUES($1,'approved','hash',$2::jsonb)",
                curriculum_id,
                json.dumps({"topics": topics}),
            )
            await conn.execute(
                "INSERT INTO engine_state(singleton,active_curriculum_id) VALUES(true,$1)",
                curriculum_id,
            )
            await conn.execute(
                "INSERT INTO study_days(study_date,budget_minutes) VALUES((now() AT TIME ZONE 'Asia/Jakarta')::date,90)"
            )
        finally:
            await conn.close()

    asyncio.run(setup())
    with TestClient(app) as test_client:
        yield test_client


def _command(update_id, command, arguments=None):
    return {
        "bot_id": "12345",
        "update_id": str(update_id),
        "actor_id": "111",
        "chat_id": "111",
        "chat_type": "private",
        "poller_fence": 1,
        "payload": {
            "kind": "command",
            "command": command,
            "arguments": arguments or [],
        },
    }


def test_today_progress_confirmation_and_planning_worker_do_not_deadlock(client):
    headers = {"Authorization": "Bearer bot-secret"}
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date().isoformat()
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_command(91, "progress", ["add", "a", "10", today]),
    )
    assert preview.status_code == 200

    async def postgres_race():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        executor = ThreadPoolExecutor(max_workers=2)
        observer = None
        try:
            token_text = await conn.fetchval(
                "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                uuid.UUID(preview.json()["operation_id"]),
            )
            confirmation = re.search(
                r"/progress confirm ([A-Za-z0-9_-]{43})", token_text
            ).group(1)
            state = await conn.fetchrow(
                "SELECT state_revision,active_curriculum_id FROM engine_state WHERE singleton=true"
            )
            operation_id, job_id = uuid.uuid4(), uuid.uuid4()
            writer = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
            try:
                await writer.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'accepted','{}'::jsonb)",
                    operation_id,
                    f"ticket16-worker:{job_id}",
                    str(job_id),
                )
                await writer.execute(
                    "UPDATE study_days SET requested_generation=1,state='replan_required' WHERE study_date=$1",
                    datetime.now(ZoneInfo("Asia/Jakarta")).date(),
                )
                await writer.execute(
                    "INSERT INTO planning_jobs(job_id,operation_id,study_date,generation,state_revision,curriculum_id,status,lease_until) VALUES($1,$2,$3,1,$4,$5,'running',now()+interval '1 hour')",
                    job_id,
                    operation_id,
                    datetime.now(ZoneInfo("Asia/Jakarta")).date(),
                    state["state_revision"],
                    state["active_curriculum_id"],
                )
            finally:
                await writer.close()

            worker_gate = conn.transaction()
            await worker_gate.start()
            await conn.fetchrow(
                "SELECT * FROM planning_jobs WHERE job_id=$1 FOR UPDATE", job_id
            )
            await conn.fetchrow(
                "SELECT * FROM engine_state WHERE singleton=true FOR UPDATE"
            )
            worker_pid = await conn.fetchval("SELECT pg_backend_pid()")
            observer = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])

            async def wait_for_row(query, description, *args):
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 10
                while loop.time() < deadline:
                    row = await observer.fetchrow(query, *args)
                    if row is not None:
                        return row
                    await asyncio.sleep(0.02)
                raise AssertionError(f"Timed out waiting for {description}")

            def progress():
                return client.post(
                    "/v1/telegram/updates",
                    headers=headers,
                    json=_command(92, "progress", ["confirm", confirmation]),
                )

            progress_future = asyncio.get_running_loop().run_in_executor(
                executor, progress
            )
            await wait_for_row(
                "SELECT pid FROM pg_stat_activity WHERE $1=ANY(pg_blocking_pids(pid)) AND pid<>$2 LIMIT 1",
                "progress request to enter its job-lock path",
                worker_pid,
                worker_pid,
            )
            await asyncio.wait_for(
                conn.fetchrow(
                    "SELECT * FROM study_days WHERE study_date=$1 FOR UPDATE",
                    datetime.now(ZoneInfo("Asia/Jakarta")).date(),
                ),
                timeout=10,
            )
            await worker_gate.commit()
            progress_result = await asyncio.wait_for(progress_future, timeout=10)
            await observer.close()
            return progress_result
        finally:
            if observer is not None:
                await observer.close()
            await conn.close()
            executor.shutdown(wait=True)

    progress_result = asyncio.run(postgres_race())
    assert progress_result.status_code == 200
    assert progress_result.json()["result"]["code"] == "PROGRESS_RECORDED"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchrow(
                    "SELECT requested_generation,state,current_plan_id FROM study_days WHERE study_date=$1",
                    datetime.now(ZoneInfo("Asia/Jakarta")).date(),
                ),
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval(
                    "SELECT count(*) FROM planning_jobs WHERE status='running'"
                ),
            )
        finally:
            await conn.close()

    day, sessions, active_jobs = asyncio.run(persisted())
    assert day["requested_generation"] == 1
    assert day["state"] == "replan_required"
    assert day["current_plan_id"] is None
    assert sessions == 1
    assert active_jobs == 1


def test_replan_and_budget_requests_coalesce_and_settle_every_waiter(client):
    headers = {"Authorization": "Bearer bot-secret"}
    responses = [
        client.post(
            "/v1/telegram/updates", headers=headers, json=_command(101, "replan")
        ),
        client.post(
            "/v1/telegram/updates",
            headers=headers,
            json=_command(102, "budget", ["45"]),
        ),
        client.post(
            "/v1/telegram/updates", headers=headers, json=_command(103, "replan")
        ),
    ]
    assert all(response.status_code == 200 for response in responses)
    operation_ids = [
        uuid.UUID(response.json()["operation_id"]) for response in responses
    ]
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()

    async def process_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                day = await conn.fetchrow(
                    "SELECT requested_generation,budget_minutes FROM study_days WHERE study_date=$1",
                    study_date,
                )
                jobs = await conn.fetch(
                    "SELECT generation,status FROM planning_jobs WHERE study_date=$1 ORDER BY generation",
                    study_date,
                )
                links = await conn.fetch(
                    "SELECT operation_id,job_id FROM planning_job_operations WHERE operation_id=ANY($1::uuid[])",
                    operation_ids,
                )
                active_job = await conn.fetchval(
                    "SELECT job_id FROM planning_jobs WHERE study_date=$1 AND status='pending'",
                    study_date,
                )
            await process_job(pool, active_job, uuid.uuid4())
            async with pool.acquire() as conn:
                results = await conn.fetch(
                    "SELECT o.operation_id,o.status,o.result,d.payload FROM operations o JOIN deliveries d USING(operation_id) WHERE o.operation_id=ANY($1::uuid[]) ORDER BY o.operation_id",
                    operation_ids,
                )
                jobs = await conn.fetch(
                    "SELECT generation,status FROM planning_jobs WHERE study_date=$1 ORDER BY generation",
                    study_date,
                )
            return day, jobs, links, results
        finally:
            await pool.close()

    day, jobs, links, results = asyncio.run(process_and_verify())
    assert day["requested_generation"] == 3
    assert day["budget_minutes"] == 45
    assert [job["generation"] for job in jobs] == [1, 2, 3]
    assert [job["status"] for job in jobs] == ["completed", "completed", "completed"]
    assert len(links) == 3
    assert len({link["job_id"] for link in links}) == 1
    assert {row["operation_id"] for row in results} == set(operation_ids)
    for row in results:
        assert row["status"] == "completed"
        assert json.loads(row["result"])["result"]["code"] == "PLAN_READY"
        assert "Today's study queue:" in json.loads(row["payload"])["text"]


def test_replacement_cancels_only_pending_blocks_in_previous_plan(client):
    headers = {"Authorization": "Bearer bot-secret"}
    first = client.post(
        "/v1/telegram/updates", headers=headers, json=_command(201, "replan")
    )
    assert first.status_code == 200

    async def initial_plan_and_states():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job_id = await conn.fetchval("SELECT job_id FROM planning_jobs")
            await process_job(pool, job_id, uuid.uuid4())
            async with pool.acquire() as conn, conn.transaction():
                plan_id = await conn.fetchval(
                    "SELECT current_plan_id FROM study_days WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                )
                blocks = await conn.fetch(
                    "SELECT block_id FROM study_blocks WHERE plan_id=$1 ORDER BY position",
                    plan_id,
                )
                assert len(blocks) >= 3
                await conn.execute(
                    "UPDATE study_blocks SET status='done' WHERE block_id=$1",
                    blocks[0]["block_id"],
                )
                await conn.execute(
                    "UPDATE study_blocks SET status='skipped' WHERE block_id=$1",
                    blocks[1]["block_id"],
                )
                return plan_id, [block["block_id"] for block in blocks]
        finally:
            await pool.close()

    old_plan_id, block_ids = asyncio.run(initial_plan_and_states())
    second = client.post(
        "/v1/telegram/updates", headers=headers, json=_command(202, "replan")
    )
    assert second.status_code == 200

    async def replace_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                new_job = await conn.fetchval(
                    "SELECT job_id FROM planning_jobs WHERE status='pending'"
                )
            await process_job(pool, new_job, uuid.uuid4())
            async with pool.acquire() as conn:
                statuses = await conn.fetch(
                    "SELECT block_id,status FROM study_blocks WHERE plan_id=$1",
                    old_plan_id,
                )
                current_plan = await conn.fetchval(
                    "SELECT current_plan_id FROM study_days WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                )
                return statuses, current_plan
        finally:
            await pool.close()

    statuses, current_plan = asyncio.run(replace_and_verify())
    assert current_plan != old_plan_id
    assert {row["block_id"]: row["status"] for row in statuses} == {
        block_ids[0]: "done",
        block_ids[1]: "skipped",
        **{block_id: "cancelled" for block_id in block_ids[2:]},
    }


def test_no_plan_retires_previous_queue_and_completes_operation(client):
    headers = {"Authorization": "Bearer bot-secret"}
    first = client.post(
        "/v1/telegram/updates", headers=headers, json=_command(301, "replan")
    )
    assert first.status_code == 200

    async def initial_plan_and_states():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job_id = await conn.fetchval("SELECT job_id FROM planning_jobs")
            await process_job(pool, job_id, uuid.uuid4())
            async with pool.acquire() as conn, conn.transaction():
                day = await conn.fetchrow(
                    "SELECT current_plan_id,queue_revision FROM study_days WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                )
                plan_id = day["current_plan_id"]
                blocks = await conn.fetch(
                    "SELECT block_id FROM study_blocks WHERE plan_id=$1 ORDER BY position",
                    plan_id,
                )
                assert len(blocks) >= 3
                await conn.execute(
                    "UPDATE study_blocks SET status='done' WHERE block_id=$1",
                    blocks[0]["block_id"],
                )
                await conn.execute(
                    "UPDATE study_blocks SET status='skipped' WHERE block_id=$1",
                    blocks[1]["block_id"],
                )
                await conn.execute(
                    "UPDATE study_days SET budget_minutes=0 WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                )
                return (
                    plan_id,
                    day["queue_revision"],
                    [block["block_id"] for block in blocks],
                )
        finally:
            await pool.close()

    old_plan_id, old_revision, block_ids = asyncio.run(initial_plan_and_states())
    second = client.post(
        "/v1/telegram/updates", headers=headers, json=_command(302, "replan")
    )
    assert second.status_code == 200

    async def no_plan_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job = await conn.fetchrow(
                    "SELECT job_id,operation_id FROM planning_jobs WHERE status='pending'"
                )
            await process_job(pool, job["job_id"], uuid.uuid4())
            async with pool.acquire() as conn:
                day = await conn.fetchrow(
                    "SELECT current_plan_id,queue_revision,state FROM study_days WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                )
                statuses = await conn.fetch(
                    "SELECT block_id,status FROM study_blocks WHERE plan_id=$1",
                    old_plan_id,
                )
                operation = await conn.fetchrow(
                    "SELECT status,result FROM operations WHERE operation_id=$1",
                    job["operation_id"],
                )
                result = json.loads(operation["result"])
                completed_job = await conn.fetchrow(
                    "SELECT result FROM planning_jobs WHERE job_id=$1", job["job_id"]
                )
                return day, statuses, operation, result, completed_job
        finally:
            await pool.close()

    day, statuses, operation, result, completed_job = asyncio.run(no_plan_and_verify())
    assert day["current_plan_id"] is None
    assert day["queue_revision"] == old_revision + 1
    assert day["state"] == "absent"
    assert {row["block_id"]: row["status"] for row in statuses} == {
        block_ids[0]: "done",
        block_ids[1]: "skipped",
        **{block_id: "cancelled" for block_id in block_ids[2:]},
    }
    assert operation["status"] == "completed"
    assert result["result"]["code"] == "REST_DAY"
    assert json.loads(completed_job["result"])["reason"] == "REST_DAY"
