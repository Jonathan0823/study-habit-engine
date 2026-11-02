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

from app.calendar import (
    CalendarAuthRequired,
    CalendarNotFound,
    CalendarTransientError,
    persist_calendar_reminder,
    sync_calendar_reminders,
)
from app.db import apply_schema
from app.main import app
from app.planning import build_graph, next_job, process_job, run_routine_tick


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


def test_model_ranking_changes_topic_priority_and_is_durably_recorded(today_client):
    async def install_curriculum_and_cursor():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
            curriculum_id = uuid.uuid4()
            topics = [
                {
                    "topic_id": topic_id,
                    "title": topic_id.upper(),
                    "description": f"Learn {topic_id}",
                    "estimate_minutes": 90,
                    "position": position,
                    "prerequisites": [],
                    "resources": [],
                }
                for position, topic_id in enumerate(("a", "b", "c"))
            ]
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

    asyncio.run(install_curriculum_and_cursor())
    accepted = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000101),
    )
    assert accepted.status_code == 200
    calls = 0

    async def ranker(candidates):
        nonlocal calls
        calls += 1
        assert {candidate["topic_id"] for candidate in candidates} == {"a", "b", "c"}
        return {
            "ranked_topics": [
                {"topic_id": topic_id, "reason_code": "NO_PREREQUISITES"}
                for topic_id in ("c", "b", "a")
            ]
        }

    async def process_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job_id = await conn.fetchval("SELECT job_id FROM planning_jobs")
            await process_job(pool, job_id, uuid.uuid4(), ranker=ranker)
            async with pool.acquire() as conn:
                blocks = await conn.fetch(
                    "SELECT b.topic_id,b.planned_minutes FROM study_blocks b JOIN study_days d ON d.current_plan_id=b.plan_id ORDER BY b.position"
                )
                attempt = await conn.fetchrow(
                    "SELECT status,provider,model,pricing_revision,reserved_usd,ranking FROM ai_attempts WHERE job_id=$1",
                    job_id,
                )
            return blocks, attempt
        finally:
            await pool.close()

    blocks, attempt = asyncio.run(process_and_verify())
    assert calls == 1
    assert [(row["topic_id"], row["planned_minutes"]) for row in blocks] == [
        ("c", 30),
        ("b", 30),
    ]
    assert attempt["status"] == "finished"
    assert attempt["provider"] == "google_gemini"
    assert attempt["reserved_usd"] == 0
    assert json.loads(attempt["ranking"])["ranked_topics"][0]["topic_id"] == "c"


def test_uncertain_gemini_dispatch_is_not_retried_after_worker_recovery(today_client):
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
    accepted = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000102),
    )
    assert accepted.status_code == 200
    calls = 0

    async def interrupted_ranker(_candidates):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    async def recover_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job_id = await conn.fetchval("SELECT job_id FROM planning_jobs")
            with pytest.raises(asyncio.CancelledError):
                await process_job(pool, job_id, uuid.uuid4(), ranker=interrupted_ranker)
            async with pool.acquire() as conn:
                status = await conn.fetchval(
                    "SELECT status FROM ai_attempts WHERE job_id=$1", job_id
                )
                await conn.execute(
                    "UPDATE planning_jobs SET lease_until=now()-interval '1 second' WHERE job_id=$1",
                    job_id,
                )
            assert status == "dispatched"

            async def must_not_retry(_candidates):
                nonlocal calls
                calls += 1
                raise AssertionError("uncertain dispatch must not be repeated")

            await process_job(pool, job_id, uuid.uuid4(), ranker=must_not_retry)
            async with pool.acquire() as conn:
                attempt = await conn.fetchrow(
                    "SELECT status,error_code,ranking FROM ai_attempts WHERE job_id=$1",
                    job_id,
                )
                plan_count = await conn.fetchval("SELECT count(*) FROM plans")
            return attempt, plan_count
        finally:
            await pool.close()

    attempt, plan_count = asyncio.run(recover_and_verify())
    assert calls == 1
    assert attempt["status"] == "dispatched"
    assert attempt["ranking"] is None
    assert plan_count == 1


@pytest.mark.parametrize(("prior_count", "prior_reserved_usd"), [(10, 0), (1, 0.000001)])
def test_ai_budget_falls_back_without_an_extra_dispatch(
    today_client, prior_count, prior_reserved_usd
):
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
    accepted = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000103),
    )
    assert accepted.status_code == 200
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()

    async def seed_daily_limit():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            for _ in range(prior_count):
                operation_id, prior_job_id = uuid.uuid4(), uuid.uuid4()
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'completed','{}'::jsonb)",
                    operation_id,
                    f"seed-ai-limit:{prior_job_id}",
                    str(prior_job_id),
                )
                await conn.execute(
                    "INSERT INTO planning_jobs(job_id,operation_id,study_date,generation,status) VALUES($1,$2,$3,$4,'completed')",
                    prior_job_id,
                    operation_id,
                    study_date,
                    100 + _,
                )
                await conn.execute(
                    "INSERT INTO ai_attempts(job_id,study_date,month_start,provider,model,pricing_revision,reserved_usd,status) VALUES($1,$2,$3,'google_gemini','gemini-test','test',$4,'finished')",
                    prior_job_id,
                    study_date,
                    study_date.replace(day=1),
                    prior_reserved_usd,
                )
        finally:
            await conn.close()

    asyncio.run(seed_daily_limit())
    calls = 0

    async def forbidden_ranker(_candidates):
        nonlocal calls
        calls += 1
        raise AssertionError("the daily call budget is already exhausted")

    async def process_and_verify():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            async with pool.acquire() as conn:
                job_id = await conn.fetchval(
                    "SELECT job_id FROM planning_jobs WHERE operation_id=$1",
                    uuid.UUID(accepted.json()["operation_id"]),
                )
            await process_job(pool, job_id, uuid.uuid4(), ranker=forbidden_ranker)
            async with pool.acquire() as conn:
                attempt_count = await conn.fetchval(
                    "SELECT count(*) FROM ai_attempts WHERE job_id=$1", job_id
                )
                blocks = await conn.fetch(
                    "SELECT b.topic_id,b.planned_minutes FROM study_blocks b JOIN study_days d ON d.current_plan_id=b.plan_id ORDER BY b.position"
                )
            return attempt_count, blocks
        finally:
            await pool.close()

    attempt_count, blocks = asyncio.run(process_and_verify())
    assert calls == 0
    assert attempt_count == 0
    assert [(row["topic_id"], row["planned_minutes"]) for row in blocks] == [
        ("a", 30),
        ("a", 30),
    ]


def test_routine_delivery_creates_today_plan_once_when_due(today_client):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)

    async def run_ticks_and_plan():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            before_due = await run_routine_tick(pool, due.replace(hour=16, minute=59))
            before_job = await next_job(pool)
            await asyncio.gather(*(run_routine_tick(pool, due) for _ in range(4)))
            scheduled_job = await next_job(pool)
            await run_routine_tick(pool, due)
            repeated_job = await next_job(pool)
            assert scheduled_job is not None
            assert repeated_job == scheduled_job
            async with pool.acquire() as conn:
                routine_operation = await conn.fetchval(
                    "SELECT operation_id FROM planning_jobs WHERE job_id=$1",
                    scheduled_job,
                )
                association_count = await conn.fetchval(
                    "SELECT count(*) FROM planning_job_operations WHERE job_id=$1 AND operation_id=$2",
                    scheduled_job,
                    routine_operation,
                )
            await process_job(pool, scheduled_job, uuid.uuid4())
            after_commit = await next_job(pool)
            return before_due, before_job, after_commit, association_count
        finally:
            await pool.close()

    before_due, before_job, after_commit, association_count = asyncio.run(
        run_ticks_and_plan()
    )
    assert before_due is False
    assert before_job is None
    assert after_commit is None
    assert association_count == 1

    delivery = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert delivery.status_code == 200
    assert delivery.json()["text"].startswith("Today's study queue:")


@pytest.mark.parametrize(
    ("case", "expected_text"),
    [
        ("no_fit", "Eligible topics exist, but no study block fits the available time"),
        ("blocked", "No eligible study topics are available right now."),
    ],
)
def test_routine_uses_approved_explanations_when_no_plan_fits(
    today_client, case, expected_text
):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)

    async def prepare_explanation():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            if case == "no_fit":
                await conn.execute(
                    "INSERT INTO study_days(study_date,budget_minutes) VALUES($1,5)",
                    study_date,
                )
            else:
                curriculum_id = await conn.fetchval(
                    "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
                )
                content = await conn.fetchval(
                    "SELECT content FROM curriculum_versions WHERE curriculum_id=$1",
                    curriculum_id,
                )
                if isinstance(content, str):
                    content = json.loads(content)
                for topic in content["topics"]:
                    await conn.execute(
                        "INSERT INTO topic_progress(topic_id,completed) VALUES($1,true)",
                        topic["topic_id"],
                    )
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await run_routine_tick(pool, due)
            job_id = await next_job(pool)
            assert job_id is not None
            await process_job(pool, job_id, uuid.uuid4())
        finally:
            await pool.close()

    asyncio.run(prepare_explanation())
    delivery = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert delivery.status_code == 200
    assert delivery.json()["text"].startswith(expected_text)


def test_routine_rest_day_does_not_create_a_plan_or_telegram_delivery(today_client):
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)

    async def set_rest_day_and_tick():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_days(study_date,budget_minutes) VALUES($1,0)",
                study_date,
            )
        finally:
            await conn.close()
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await run_routine_tick(pool, due)
            return await next_job(pool)
        finally:
            await pool.close()

    assert asyncio.run(set_rest_day_and_tick()) is None
    response = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert response.status_code == 204


def _seed_calendar_plan(today_client):
    async def seed():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
            async with pool.acquire() as conn, conn.transaction():
                state = await conn.fetchrow(
                    "SELECT active_curriculum_id,state_revision FROM engine_state WHERE singleton=true"
                )
                curriculum = await conn.fetchrow(
                    "SELECT content FROM curriculum_versions WHERE curriculum_id=$1",
                    state["active_curriculum_id"],
                )
                content = curriculum["content"]
                if isinstance(content, str):
                    content = json.loads(content)
                topic = content["topics"][0]
                plan_id = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO study_days(study_date,requested_generation) VALUES($1,1)",
                    study_date,
                )
                await conn.execute(
                    """INSERT INTO plans(plan_id,study_date,curriculum_id,generation,state_revision,queue_revision)
                       VALUES($1,$2,$3,1,$4,1)""",
                    plan_id,
                    study_date,
                    state["active_curriculum_id"],
                    state["state_revision"],
                )
                await conn.execute(
                    """INSERT INTO study_blocks(block_id,plan_id,position,topic_id,planned_minutes)
                       VALUES($1,$2,0,$3,25)""",
                    uuid.uuid4(),
                    plan_id,
                    topic["topic_id"],
                )
                await conn.execute(
                    """UPDATE study_days SET state='valid',current_plan_id=$2,queue_revision=1
                       WHERE study_date=$1""",
                    study_date,
                    plan_id,
                )
                await persist_calendar_reminder(
                    conn,
                    study_date,
                    plan_id,
                    content,
                    [{"topic_id": topic["topic_id"], "planned_minutes": 25}],
                )
            return study_date
        finally:
            await pool.close()

    return asyncio.run(seed())


class _FakeCalendar:
    def __init__(self):
        self.events = {}
        self.calls = []

    async def request(self, method, event_id=None, *, body=None):
        self.calls.append((method, event_id, body))
        if method == "GET":
            if event_id not in self.events:
                raise CalendarNotFound("missing")
            return self.events[event_id]
        if method == "POST":
            self.events[body["id"]] = dict(body)
            return self.events[body["id"]]
        if method == "PATCH":
            self.events[event_id].update(body)
            return self.events[event_id]
        if method == "DELETE":
            self.events.pop(event_id, None)
            return None
        raise AssertionError(method)


def test_routine_delivery_reuses_valid_queue_without_replanning(today_client):
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
    replan = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json={
            **_today_payload(900000104),
            "payload": {"kind": "command", "command": "replan", "arguments": []},
        },
    )
    assert replan.status_code == 200

    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)

    async def prepare_and_send_user_replan():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            job_id = await next_job(pool)
            assert job_id is not None
            await process_job(pool, job_id, uuid.uuid4())
        finally:
            await pool.close()

    asyncio.run(prepare_and_send_user_replan())

    async def read_calendar_intent():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchrow(
                "SELECT topics FROM calendar_reminders WHERE study_date=$1",
                study_date,
            )
        finally:
            await conn.close()

    calendar_intent = asyncio.run(read_calendar_intent())
    assert calendar_intent is not None
    calendar_topics = calendar_intent["topics"]
    if isinstance(calendar_topics, str):
        calendar_topics = json.loads(calendar_topics)
    assert calendar_topics
    assert all(set(topic) == {"title", "planned_minutes"} for topic in calendar_topics)

    user_delivery = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert user_delivery.status_code == 200
    sent = today_client.post(
        f"/v1/deliveries/{user_delivery.json()['delivery_id']}/result",
        headers={"Authorization": "Bearer bot-secret"},
        json={
            "fence": user_delivery.json()["fence"],
            "outcome": "sent",
            "telegram_receipt": "replan-message-1",
        },
    )
    assert sent.status_code == 200

    async def tick_existing_plan():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await asyncio.gather(*(run_routine_tick(pool, due) for _ in range(4)))
            return await next_job(pool)
        finally:
            await pool.close()

    assert asyncio.run(tick_existing_plan()) is None
    routine_delivery = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert routine_delivery.status_code == 200
    assert routine_delivery.json()["text"].startswith("Today's study queue:")

    fake_calendar = _FakeCalendar()

    async def sync_replan_and_check_manual_deletion():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await sync_calendar_reminders(pool, fake_calendar, due - timedelta(minutes=5))
            event_id, initial = next(iter(fake_calendar.events.items()))
            async with pool.acquire() as conn, conn.transaction():
                current = await conn.fetchrow(
                    """SELECT p.plan_id,p.curriculum_id,v.content,b.topic_id
                       FROM study_days d JOIN plans p ON p.plan_id=d.current_plan_id
                       JOIN curriculum_versions v ON v.curriculum_id=p.curriculum_id
                       JOIN study_blocks b ON b.plan_id=p.plan_id AND b.status='pending'
                       WHERE d.study_date=$1 ORDER BY b.position LIMIT 1""",
                    study_date,
                )
                await conn.execute(
                    "UPDATE study_blocks SET planned_minutes=30 WHERE plan_id=$1 AND topic_id=$2 AND status='pending'",
                    current["plan_id"],
                    current["topic_id"],
                )
                await persist_calendar_reminder(
                    conn,
                    study_date,
                    current["plan_id"],
                    current["content"],
                    [{"topic_id": current["topic_id"], "planned_minutes": 30}],
                )
            await sync_calendar_reminders(
                pool, fake_calendar, due - timedelta(minutes=4)
            )
            updated = fake_calendar.events[event_id]
            fake_calendar.events.clear()
            await sync_calendar_reminders(pool, fake_calendar, due)
            state = await pool.fetchval(
                "SELECT state FROM calendar_reminders WHERE study_date=$1", study_date
            )
            return initial, updated, state
        finally:
            await pool.close()

    initial_event, updated_event, calendar_state = asyncio.run(
        sync_replan_and_check_manual_deletion()
    )
    assert initial_event["visibility"] == "private"
    assert "resources" not in initial_event["description"]
    assert "https://" not in initial_event["description"]
    assert "30 min" in updated_event["description"]
    assert len([call for call in fake_calendar.calls if call[0] == "POST"]) == 1
    assert len([call for call in fake_calendar.calls if call[0] == "PATCH"]) == 1
    assert calendar_state == "suppressed"


def test_stale_planning_job_expires_without_a_catchup_delivery(today_client):
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
    accepted = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json={
            **_today_payload(900000107),
            "payload": {"kind": "command", "command": "replan", "arguments": []},
        },
    )
    assert accepted.status_code == 200
    study_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    stale_date = study_date - timedelta(days=1)

    async def expire_stale_job():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            job_id = await next_job(pool)
            assert job_id is not None
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO study_days(study_date,requested_generation) VALUES($1,1)",
                    stale_date,
                )
                await conn.execute(
                    "UPDATE planning_jobs SET study_date=$2 WHERE job_id=$1",
                    job_id,
                    stale_date,
                )
            await process_job(pool, job_id, uuid.uuid4())
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT count(*) FROM deliveries WHERE study_date=$1", stale_date
                )
        finally:
            await pool.close()

    assert asyncio.run(expire_stale_job()) == 0
    claim = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert claim.status_code == 204


def test_early_today_delivery_suppresses_duplicate_routine_telegram(today_client):
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
    today = today_client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_today_payload(900000105),
    )
    assert today.status_code == 200

    async def process_today():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            job_id = await next_job(pool)
            assert job_id is not None
            await process_job(pool, job_id, uuid.uuid4())
        finally:
            await pool.close()

    asyncio.run(process_today())
    user_delivery = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert user_delivery.status_code == 200
    sent = today_client.post(
        f"/v1/deliveries/{user_delivery.json()['delivery_id']}/result",
        headers={"Authorization": "Bearer bot-secret"},
        json={
            "fence": user_delivery.json()["fence"],
            "outcome": "sent",
            "telegram_receipt": "early-today-message-1",
        },
    )
    assert sent.status_code == 200

    due = datetime.combine(
        datetime.now(ZoneInfo("Asia/Jakarta")).date(),
        datetime.min.time(),
        ZoneInfo("Asia/Jakarta"),
    ).replace(hour=17)

    async def tick_routine():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await run_routine_tick(pool, due)
            await run_routine_tick(pool, due)
            return await next_job(pool)
        finally:
            await pool.close()

    assert asyncio.run(tick_routine()) is None
    duplicate = today_client.post(
        "/v1/deliveries/claim", headers={"Authorization": "Bearer bot-secret"}
    )
    assert duplicate.status_code == 204


def test_calendar_restores_cancelled_unconfirmed_app_event(today_client):
    study_date = _seed_calendar_plan(today_client)
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)
    fake_calendar = _FakeCalendar()

    async def restore():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            event_id = await pool.fetchval(
                "SELECT event_id FROM calendar_reminders WHERE study_date=$1", study_date
            )
            fake_calendar.events[event_id] = {
                "id": event_id,
                "status": "cancelled",
                "extendedProperties": {
                    "private": {
                        "app": "study-habit-engine",
                        "study_date": study_date.isoformat(),
                    }
                },
            }
            await sync_calendar_reminders(
                pool, fake_calendar, due - timedelta(minutes=5)
            )
            state = await pool.fetchval(
                "SELECT state FROM calendar_reminders WHERE study_date=$1", study_date
            )
            return state, fake_calendar.events[event_id]
        finally:
            await pool.close()

    state, restored = asyncio.run(restore())
    assert state == "synced"
    assert restored["status"] == "confirmed"
    assert [call[0] for call in fake_calendar.calls] == ["GET", "PATCH"]
    assert fake_calendar.calls[-1][2]["status"] == "confirmed"


def test_calendar_suppresses_cancelled_previously_synced_event(today_client):
    study_date = _seed_calendar_plan(today_client)
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)
    fake_calendar = _FakeCalendar()

    async def suppress():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            event_id = await pool.fetchval(
                "SELECT event_id FROM calendar_reminders WHERE study_date=$1", study_date
            )
            await pool.execute(
                "UPDATE calendar_reminders SET state='synced',ever_synced=true,next_check_at=$2 WHERE study_date=$1",
                study_date,
                due,
            )
            fake_calendar.events[event_id] = {
                "id": event_id,
                "status": "cancelled",
            }
            await sync_calendar_reminders(pool, fake_calendar, due)
            state = await pool.fetchval(
                "SELECT state FROM calendar_reminders WHERE study_date=$1", study_date
            )
            return state
        finally:
            await pool.close()

    assert asyncio.run(suppress()) == "suppressed"
    assert [call[0] for call in fake_calendar.calls] == ["GET"]


def test_calendar_event_is_removed_when_queue_finishes_before_17(today_client):
    study_date = _seed_calendar_plan(today_client)
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)
    fake_calendar = _FakeCalendar()

    async def finish_and_sync():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            await sync_calendar_reminders(pool, fake_calendar, due - timedelta(minutes=5))
            event_id = next(iter(fake_calendar.events))
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE study_blocks SET status='done' WHERE plan_id=(SELECT current_plan_id FROM study_days WHERE study_date=$1)",
                    study_date,
                )
            await sync_calendar_reminders(
                pool, fake_calendar, due - timedelta(minutes=4)
            )
            state = await pool.fetchval(
                "SELECT state FROM calendar_reminders WHERE study_date=$1", study_date
            )
            return state, event_id in fake_calendar.events
        finally:
            await pool.close()

    state, event_remains = asyncio.run(finish_and_sync())
    assert state == "cancelled"
    assert event_remains is False
    assert any(call[0] == "DELETE" for call in fake_calendar.calls)


def test_calendar_transient_failures_stop_after_one_hour(today_client):
    study_date = _seed_calendar_plan(today_client)
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)
    first_attempt = due - timedelta(minutes=5)
    retry_attempt = first_attempt + timedelta(minutes=2)

    class FailingCalendar:
        async def request(self, method, event_id=None, *, body=None):
            if method == "GET":
                raise CalendarNotFound("not created")
            raise CalendarTransientError("temporary outage")

    async def try_twice():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        try:
            client = FailingCalendar()
            await sync_calendar_reminders(pool, client, first_attempt)
            async with pool.acquire() as conn:
                first = await conn.fetchrow(
                    "SELECT state,retry_until,next_retry_at,attempts FROM calendar_reminders WHERE study_date=$1",
                    study_date,
                )
                await conn.execute(
                    "UPDATE calendar_reminders SET retry_until=$2,next_retry_at=$2 WHERE study_date=$1",
                    study_date,
                    retry_attempt - timedelta(seconds=1),
                )
            await sync_calendar_reminders(pool, client, retry_attempt)
            async with pool.acquire() as conn:
                final = await conn.fetchrow(
                    "SELECT state,last_error_code,attempts FROM calendar_reminders WHERE study_date=$1",
                    study_date,
                )
            return first, final
        finally:
            await pool.close()

    first, final = asyncio.run(try_twice())
    assert first["state"] == "pending"
    assert first["retry_until"] is not None
    assert first["next_retry_at"] is not None
    assert first["attempts"] == 1
    assert final["state"] == "failed"
    assert final["last_error_code"] == "RETRY_EXHAUSTED"
    assert final["attempts"] == 2


def test_calendar_reauthorization_resumes_only_after_secret_file_changes(today_client, tmp_path):
    study_date = _seed_calendar_plan(today_client)
    due = datetime.combine(
        study_date, datetime.min.time(), ZoneInfo("Asia/Jakarta")
    ).replace(hour=17)
    credentials_file = tmp_path / "calendar.json"

    class ReconnectableCalendar(_FakeCalendar):
        def __init__(self):
            super().__init__()
            self.credentials_file = credentials_file
            self.authorized = False

        async def request(self, method, event_id=None, *, body=None):
            if not self.authorized:
                raise CalendarAuthRequired("reconnect required")
            return await super().request(method, event_id, body=body)

    async def reconnect():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        client = ReconnectableCalendar()
        try:
            await sync_calendar_reminders(pool, client, due - timedelta(minutes=5))
            async with pool.acquire() as conn:
                first_state = await conn.fetchval(
                    "SELECT state FROM calendar_reminders WHERE study_date=$1", study_date
                )
            credentials_file.write_text('{"client_id":"private"}')
            credentials_file.chmod(0o600)
            client.authorized = True
            await sync_calendar_reminders(pool, client, due - timedelta(minutes=4))
            async with pool.acquire() as conn:
                final_state = await conn.fetchval(
                    "SELECT state FROM calendar_reminders WHERE study_date=$1", study_date
                )
            return first_state, final_state, client.calls
        finally:
            await pool.close()

    first_state, final_state, calls = asyncio.run(reconnect())
    assert first_state == "auth_required"
    assert final_state == "synced"
    assert [call[0] for call in calls] == ["GET", "POST"]
