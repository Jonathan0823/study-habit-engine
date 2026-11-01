import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg
import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.db import apply_schema
from app.main import app
from app.planning import build_graph, next_job, process_job


@pytest.fixture
def today_client():
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
            topics = [
                {
                    "topic_id": "a",
                    "title": "Arrays",
                    "description": "Learn arrays",
                    "estimate_minutes": 15,
                    "position": 0,
                    "prerequisites": [],
                    "resources": [
                        {"url": "https://example.org/a", "title": "Arrays resource"}
                    ],
                },
                {
                    "topic_id": "b",
                    "title": "Trees",
                    "description": "Learn trees",
                    "estimate_minutes": 90,
                    "position": 1,
                    "prerequisites": ["a"],
                    "resources": [
                        {"url": "https://example.org/b", "title": "Trees resource"}
                    ],
                },
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
        finally:
            await conn.close()

    asyncio.run(setup())
    with TestClient(app) as client:
        yield client


def _today_payload(update_id):
    return {
        "bot_id": "12345",
        "update_id": str(update_id),
        "actor_id": "111",
        "chat_id": "111",
        "chat_type": "private",
        "poller_fence": 1,
        "payload": {"kind": "command", "command": "today", "arguments": []},
    }


def test_study_day_current_plan_references_an_existing_plan(today_client):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()

    async def verify_constraint():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_days(study_date) VALUES($1)", study_date
            )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "UPDATE study_days SET current_plan_id=$2 WHERE study_date=$1",
                    study_date,
                    uuid.uuid4(),
                )
        finally:
            await conn.close()

    asyncio.run(verify_constraint())


def test_repeated_today_coalesces_job_and_settles_each_operation(today_client):
    async def setup_cursor():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
        finally:
            await conn.close()

    asyncio.run(setup_cursor())
    headers = {"Authorization": "Bearer bot-secret"}
    assert (
        today_client.post(
            "/v1/telegram/updates", headers=headers, json=_today_payload(900000010)
        ).status_code
        == 200
    )
    assert (
        today_client.post(
            "/v1/telegram/updates", headers=headers, json=_today_payload(900000011)
        ).status_code
        == 200
    )

    async def process_and_verify():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            jobs = await conn.fetch("SELECT job_id FROM planning_jobs")
            operation_ids = await conn.fetch(
                "SELECT operation_id FROM telegram_ingestion ORDER BY operation_id"
            )
        finally:
            await conn.close()
        assert len(jobs) == 1
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await process_job(pool, jobs[0]["job_id"], uuid.uuid4())
            async with pool.acquire() as conn:
                results = await conn.fetch(
                    "SELECT o.operation_id,o.status,o.result,d.payload FROM operations o LEFT JOIN deliveries d ON d.operation_id=o.operation_id AND d.kind='today' WHERE o.operation_id=ANY($1::uuid[]) ORDER BY o.operation_id",
                    [row["operation_id"] for row in operation_ids],
                )
                return results
        finally:
            await pool.close()

    results = asyncio.run(process_and_verify())
    assert len(results) == 2
    for row in results:
        assert row["status"] == "completed"
        assert json.loads(row["result"])["result"]["code"] == "PLAN_READY"
        assert "Today's study queue:" in json.loads(row["payload"])["text"]

    response = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000012),
    )
    assert response.status_code == 200

    async def verify_existing_plan_operation():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            row = await conn.fetchrow(
                "SELECT status,result FROM operations WHERE operation_id=$1",
                uuid.UUID(response.json()["operation_id"]),
            )
            return row["status"], json.loads(row["result"])
        finally:
            await conn.close()

    status, result = asyncio.run(verify_existing_plan_operation())
    assert status == "completed"
    assert result["status"] == "completed"
    assert result["result"]["code"] == "PLAN_READY"
    assert result["result"]["plan_id"]


def test_today_on_replan_required_explains_how_to_refresh_without_enqueuing(
    today_client,
):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()

    async def setup():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
            await conn.execute(
                "INSERT INTO study_days(study_date,state,requested_generation) VALUES($1,'replan_required',3)",
                study_date,
            )
        finally:
            await conn.close()

    asyncio.run(setup())
    response = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000002),
    )
    assert response.status_code == 200

    async def verify():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            jobs = await conn.fetchval("SELECT count(*) FROM planning_jobs")
            delivery = await conn.fetchrow(
                "SELECT payload FROM deliveries WHERE kind='today'"
            )
            return jobs, json.loads(delivery["payload"])["text"]
        finally:
            await conn.close()

    jobs, text = asyncio.run(verify())
    assert jobs == 0
    assert "/replan" in text

    explicit = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json={
            **_today_payload(900000003),
            "payload": {"kind": "command", "command": "replan", "arguments": []},
        },
    )
    assert explicit.status_code == 200
    assert explicit.json()["status"] == "accepted"

    async def verify_replan():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchrow(
                "SELECT d.requested_generation,j.status,j.generation FROM study_days d JOIN planning_jobs j ON j.study_date=d.study_date WHERE d.study_date=$1",
                study_date,
            )
        finally:
            await conn.close()

    replanned = asyncio.run(verify_replan())
    assert replanned["requested_generation"] == 4
    assert replanned["status"] == "pending"
    assert replanned["generation"] == 4


def test_superseded_accepted_job_settles_operation_and_delivery(today_client):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    operation_id, job_id = uuid.uuid4(), uuid.uuid4()

    async def setup():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_days(study_date,requested_generation) VALUES($1,2)",
                study_date,
            )
            await conn.execute(
                "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,'hash','accepted',$3::jsonb)",
                operation_id,
                f"test:{operation_id}",
                json.dumps({"status": "accepted", "result": {"code": "PLAN_ACCEPTED"}}),
            )
            await conn.execute(
                "INSERT INTO planning_jobs(job_id,operation_id,study_date,generation) VALUES($1,$2,$3,1)",
                job_id,
                operation_id,
                study_date,
            )
        finally:
            await conn.close()

    asyncio.run(setup())

    async def process_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await process_job(pool, job_id, uuid.uuid4())
            async with pool.acquire() as conn:
                operation = await conn.fetchrow(
                    "SELECT status,result FROM operations WHERE operation_id=$1",
                    operation_id,
                )
                job = await conn.fetchrow(
                    "SELECT status,result FROM planning_jobs WHERE job_id=$1", job_id
                )
                delivery = await conn.fetchrow(
                    "SELECT payload FROM deliveries WHERE operation_id=$1", operation_id
                )
                return operation, job, delivery
        finally:
            await pool.close()

    operation, job, delivery = asyncio.run(process_and_verify())
    operation_result = json.loads(operation["result"])
    assert operation["status"] == "completed"
    assert operation_result["status"] == "completed"
    assert operation_result["result"] == {
        "code": "PLAN_SUPERSEDED",
        "reason": "PLAN_SUPERSEDED",
    }
    assert job["status"] == "completed"
    assert json.loads(job["result"]) == {"reason": "PLAN_SUPERSEDED"}
    assert delivery is not None


def test_worker_restart_polls_and_reclaims_expired_running_job(today_client):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    operation_id, job_id = uuid.uuid4(), uuid.uuid4()

    async def setup():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_days(study_date,requested_generation) VALUES($1,1)",
                study_date,
            )
            await conn.execute(
                "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,'hash','accepted',$3::jsonb)",
                operation_id,
                f"test:{operation_id}",
                json.dumps({"status": "accepted", "result": {"code": "PLAN_ACCEPTED"}}),
            )
            await conn.execute(
                "INSERT INTO planning_jobs(job_id,operation_id,study_date,generation,status,lease_owner,fence,lease_until) VALUES($1,$2,$3,1,'running',$4,7,now()-interval '1 second')",
                job_id,
                operation_id,
                study_date,
                uuid.uuid4(),
            )
        finally:
            await conn.close()

    asyncio.run(setup())

    async def restart_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            selected = await next_job(pool)
            assert selected == job_id
            await process_job(pool, selected, uuid.uuid4())
            async with pool.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT status,fence,lease_until FROM planning_jobs WHERE job_id=$1",
                    job_id,
                )
        finally:
            await pool.close()

    reclaimed = asyncio.run(restart_and_verify())
    assert reclaimed["status"] == "completed"
    assert reclaimed["fence"] == 8
    assert reclaimed["lease_until"] is None


def test_historical_study_marks_topic_started_without_spending_daily_budget(
    today_client,
):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()

    async def setup():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
            curriculum = uuid.uuid4()
            topics = [
                {
                    "topic_id": "a",
                    "title": "Arrays",
                    "description": "Learn arrays",
                    "estimate_minutes": 15,
                    "position": 0,
                    "prerequisites": [],
                    "resources": [],
                },
                {
                    "topic_id": "b",
                    "title": "Trees",
                    "description": "Learn trees",
                    "estimate_minutes": 90,
                    "position": 1,
                    "prerequisites": [],
                    "resources": [],
                },
            ]
            await conn.execute(
                "INSERT INTO curriculum_versions(curriculum_id,status,content_hash,content) VALUES($1,'approved',$2,$3::jsonb)",
                curriculum,
                f"test-{curriculum}",
                json.dumps({"topics": topics}),
            )
            await conn.execute(
                "UPDATE engine_state SET active_curriculum_id=$1 WHERE singleton=true",
                curriculum,
            )
            await conn.execute(
                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,'b',$2,7),($3,'outside',$4,10)",
                uuid.uuid4(),
                study_date - timedelta(days=1),
                uuid.uuid4(),
                study_date,
            )
        finally:
            await conn.close()

    asyncio.run(setup())
    response = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000003),
    )
    assert response.status_code == 200

    async def run_worker():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            job_id = await conn.fetchval("SELECT job_id FROM planning_jobs")
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        async with await psycopg.AsyncConnection.connect(
            os.environ["DATABASE_URL"], autocommit=True
        ) as checkpoint_conn:
            await build_graph(
                AsyncPostgresSaver(checkpoint_conn), pool, uuid.uuid4()
            ).ainvoke(
                {"job_id": str(job_id)}, {"configurable": {"thread_id": str(job_id)}}
            )
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT b.topic_id,b.planned_minutes FROM study_blocks b JOIN plans p USING(plan_id) ORDER BY b.position"
            )
            return rows

    rows = asyncio.run(run_worker())
    assert rows[0]["topic_id"] == "b"
    assert sum(row["planned_minutes"] for row in rows) <= 50


@pytest.mark.parametrize(
    ("capacity", "prerequisites", "expected"),
    [(10, [], "NO_BLOCK_FITS"), (60, ["missing"], "NO_ELIGIBLE_TOPICS")],
)
def test_no_plan_reason_distinguishes_capacity_from_eligibility(
    today_client, capacity, prerequisites, expected
):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    operation_id, job_id = uuid.uuid4(), uuid.uuid4()

    async def run():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn, conn.transaction():
                curriculum = uuid.uuid4()
                topic = {
                    "topic_id": "a",
                    "title": "Arrays",
                    "description": "Learn arrays",
                    "estimate_minutes": 15,
                    "position": 0,
                    "prerequisites": prerequisites,
                    "resources": [],
                }
                await conn.execute(
                    "INSERT INTO curriculum_versions(curriculum_id,status,content_hash,content) VALUES($1,'approved',$2,$3::jsonb)",
                    curriculum,
                    f"test-{curriculum}",
                    json.dumps({"topics": [topic]}),
                )
                await conn.execute(
                    "UPDATE engine_state SET active_curriculum_id=$1 WHERE singleton=true",
                    curriculum,
                )
                await conn.execute(
                    "INSERT INTO study_days(study_date,budget_minutes,requested_generation) VALUES($1,$2,1)",
                    study_date,
                    capacity,
                )
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,'hash','accepted',$3::jsonb)",
                    operation_id,
                    f"test:{operation_id}",
                    json.dumps(
                        {"status": "accepted", "result": {"code": "PLAN_ACCEPTED"}}
                    ),
                )
                await conn.execute(
                    "INSERT INTO planning_jobs(job_id,operation_id,study_date,generation) VALUES($1,$2,$3,1)",
                    job_id,
                    operation_id,
                    study_date,
                )
            await process_job(pool, job_id, uuid.uuid4())
            async with pool.acquire() as conn:
                operation = await conn.fetchrow(
                    "SELECT result FROM operations WHERE operation_id=$1", operation_id
                )
                delivery = await conn.fetchrow(
                    "SELECT payload FROM deliveries WHERE operation_id=$1", operation_id
                )
                return json.loads(operation["result"])["result"]["code"], json.loads(
                    delivery["payload"]
                )["text"]
        finally:
            await pool.close()

    code, text = asyncio.run(run())
    assert code == expected
    if expected == "NO_BLOCK_FITS":
        assert "Eligible topics exist" in text
    else:
        assert "No eligible study topics" in text


def test_replan_does_not_refill_a_topic_skipped_from_todays_queue(today_client):
    async def setup():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
            topics = [
                {
                    "topic_id": topic_id,
                    "title": topic_id,
                    "description": topic_id,
                    "estimate_minutes": 60,
                    "position": position,
                    "prerequisites": [],
                    "resources": [],
                }
                for position, topic_id in enumerate(
                    ("skipped-topic", "available-topic")
                )
            ]
            curriculum_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO curriculum_versions(curriculum_id,status,content_hash,content) VALUES($1,'approved',$2,$3::jsonb)",
                curriculum_id,
                f"test-{curriculum_id}",
                json.dumps({"topics": topics}),
            )
            await conn.execute(
                "UPDATE engine_state SET active_curriculum_id=$1 WHERE singleton=true",
                curriculum_id,
            )
        finally:
            await conn.close()

    asyncio.run(setup())
    headers = {"Authorization": "Bearer bot-secret"}
    first = today_client.post(
        "/v1/telegram/updates", headers=headers, json=_today_payload(900000001)
    )
    assert first.status_code == 200

    async def create_initial_plan():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job_id = await conn.fetchval("SELECT job_id FROM planning_jobs")
            await process_job(pool, job_id, uuid.uuid4())
            async with pool.acquire() as conn, conn.transaction():
                plan_id = await conn.fetchval(
                    "SELECT current_plan_id FROM study_days WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                )
                skipped_block = await conn.fetchval(
                    "SELECT block_id FROM study_blocks WHERE plan_id=$1 AND topic_id='skipped-topic' LIMIT 1",
                    plan_id,
                )
                assert skipped_block is not None
                await conn.execute(
                    "UPDATE study_blocks SET status='skipped' WHERE block_id=$1",
                    skipped_block,
                )
        finally:
            await pool.close()

    asyncio.run(create_initial_plan())
    replan_payload = _today_payload(900000002)
    replan_payload["payload"]["command"] = "replan"
    replanned = today_client.post(
        "/v1/telegram/updates", headers=headers, json=replan_payload
    )
    assert replanned.status_code == 200

    async def run_replan_worker():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job_id = await conn.fetchval(
                    "SELECT job_id FROM planning_jobs WHERE status='pending'"
                )
            await process_job(pool, job_id, uuid.uuid4())
            async with pool.acquire() as conn:
                return await conn.fetch(
                    "SELECT b.topic_id FROM study_blocks b JOIN study_days d ON d.current_plan_id=b.plan_id ORDER BY b.position"
                )
        finally:
            await pool.close()

    blocks = asyncio.run(run_replan_worker())
    assert blocks
    assert {block["topic_id"] for block in blocks} == {"available-topic"}


def test_today_worker_commits_only_eligible_queue_and_outbox(today_client):
    async def run():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
        finally:
            await conn.close()

    asyncio.run(run())
    response = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json={
            "bot_id": "12345",
            "update_id": "900000001",
            "actor_id": "111",
            "chat_id": "111",
            "chat_type": "private",
            "poller_fence": 1,
            "payload": {"kind": "command", "command": "today", "arguments": []},
        },
    )
    assert response.status_code == 200

    async def run_job():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            job_id = await conn.fetchval("SELECT job_id FROM planning_jobs")
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        owner = uuid.uuid4()
        async with await psycopg.AsyncConnection.connect(
            os.environ["DATABASE_URL"], autocommit=True
        ) as checkpoint_conn:
            saver = AsyncPostgresSaver(checkpoint_conn)
            graph = build_graph(saver, pool, owner)
            await graph.ainvoke(
                {"job_id": str(job_id)}, {"configurable": {"thread_id": str(job_id)}}
            )
        await pool.close()
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            rows = await conn.fetch(
                "SELECT b.topic_id,b.planned_minutes FROM study_blocks b JOIN plans p USING(plan_id) ORDER BY b.position"
            )
            delivery = await conn.fetchrow(
                "SELECT payload,operation_id FROM deliveries WHERE kind='today' ORDER BY created_at LIMIT 1"
            )
            operation_status = await conn.fetchval(
                "SELECT status FROM operations WHERE operation_id=$1",
                delivery["operation_id"],
            )
            return rows, delivery, operation_status
        finally:
            await conn.close()

    blocks, delivery, operation_status = asyncio.run(run_job())
    assert [(row["topic_id"], row["planned_minutes"]) for row in blocks] == [
        ("a", 30),
        ("a", 30),
    ]
    assert (
        "Arrays resource: https://example.org/a"
        in json.loads(delivery["payload"])["text"]
    )
    assert "Trees" not in json.loads(delivery["payload"])["text"]
    assert operation_status == "completed"

    valid_response = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000002),
    )
    assert valid_response.status_code == 200

    async def verify_valid_delivery():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT payload->>'text' FROM deliveries WHERE kind='today' ORDER BY created_at DESC LIMIT 1"
            )
        finally:
            await conn.close()

    assert asyncio.run(verify_valid_delivery()).splitlines() == [
        "Today's study queue:",
        "1. Arrays — 30 min",
        "Arrays resource: https://example.org/a",
        "2. Arrays — 30 min",
        "Arrays resource: https://example.org/a",
    ]
