import asyncio
import json
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.db import apply_schema
from app.main import app, parse_actual_minutes


@pytest.mark.parametrize("value", ["0", "1", "30", "1440"])
def test_actual_minutes_accepts_integer_truth(value):
    assert parse_actual_minutes([value]) == (int(value), None)


@pytest.mark.parametrize(
    "arguments",
    [[], ["-1"], ["1441"], ["1.5"], ["+1"], ["１２"], ["30", "40"]],
)
def test_actual_minutes_rejects_missing_non_integer_or_out_of_range(arguments):
    value, error = parse_actual_minutes(arguments)
    assert value is None
    assert error == "INVALID_ACTUAL_MINUTES"


@pytest.fixture
def block_client():
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL integration test requires MIGRATION_DATABASE_URL")
    os.environ.update(
        BOT_SERVICE_TOKEN="bot-secret",
        TELEGRAM_BOT_ID="12345",
        TELEGRAM_USER_ID="111",
        TELEGRAM_PRIVATE_CHAT_ID="111",
    )
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    curriculum_id, plan_id = uuid.uuid4(), uuid.uuid4()
    first_block, second_block = uuid.uuid4(), uuid.uuid4()

    async def setup():
        await apply_schema(url)
        conn = await asyncpg.connect(url)
        try:
            await conn.execute(
                "TRUNCATE planning_jobs, plans, study_days, study_sessions, study_blocks, topic_progress, deliveries, telegram_ingestion, operations, telegram_cursor, curriculum_versions, engine_state CASCADE"
            )
            content = {
                "topics": [
                    {
                        "topic_id": "arrays",
                        "title": "Arrays",
                        "description": "Arrays",
                        "estimate_minutes": 60,
                        "position": 0,
                        "prerequisites": [],
                        "resources": [],
                    }
                ]
            }
            await conn.execute(
                "INSERT INTO curriculum_versions(curriculum_id,status,content_hash,content) VALUES($1,'approved','hash',$2::jsonb)",
                curriculum_id,
                json.dumps(content),
            )
            await conn.execute(
                "INSERT INTO engine_state(singleton,active_curriculum_id) VALUES(true,$1)",
                curriculum_id,
            )
            await conn.execute(
                "INSERT INTO study_days(study_date,budget_minutes,state,queue_revision) VALUES($1,60,'valid',1)",
                today,
            )
            await conn.execute(
                "INSERT INTO plans(plan_id,study_date,curriculum_id,generation,state_revision,queue_revision) VALUES($1,$2,$3,1,0,1)",
                plan_id,
                today,
                curriculum_id,
            )
            await conn.execute(
                "UPDATE study_days SET current_plan_id=$2 WHERE study_date=$1",
                today,
                plan_id,
            )
            await conn.execute(
                "INSERT INTO study_blocks(block_id,plan_id,position,topic_id,planned_minutes) VALUES($1,$2,0,'arrays',30),($3,$2,1,'arrays',30)",
                first_block,
                plan_id,
                second_block,
            )
            await conn.execute(
                "INSERT INTO telegram_cursor(bot_id,next_offset,poller_owner,fence,lease_until) VALUES(12345,0,$1,1,now()+interval '1 minute')",
                uuid.uuid4(),
            )
        finally:
            await conn.close()

    asyncio.run(setup())
    with TestClient(app) as client:
        yield client, first_block, second_block


def _payload(update_id, command, arguments):
    return {
        "bot_id": "12345",
        "update_id": str(update_id),
        "actor_id": "111",
        "chat_id": "111",
        "chat_type": "private",
        "poller_fence": 1,
        "payload": {"kind": "command", "command": command, "arguments": arguments},
    }


def test_done_prefills_duration_records_once_and_invalidates_over_budget_queue(
    block_client,
):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    choice = client.post(
        "/v1/telegram/updates", headers=headers, json=_payload(100, "done", [])
    )
    assert choice.status_code == 200
    assert choice.json()["status"] == "accepted"

    async def choice_text():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                uuid.UUID(choice.json()["operation_id"]),
            )
        finally:
            await conn.close()

    assert "30 min" in asyncio.run(choice_text())

    args = [str(block_id), "1", "40"]
    update = _payload(101, "done", args)
    done = client.post("/v1/telegram/updates", headers=headers, json=update)
    replay = client.post("/v1/telegram/updates", headers=headers, json=update)
    assert done.json()["operation_id"] == replay.json()["operation_id"]
    assert replay.json()["status"] == "completed"
    assert replay.json()["result"]["code"] == "REPLAN_REQUIRED"

    async def delivery_text():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                uuid.UUID(done.json()["operation_id"]),
            )
        finally:
            await conn.close()

    assert "/replan" in asyncio.run(delivery_text())

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT count(*) FROM study_sessions WHERE block_id=$1", block_id
                ),
                await conn.fetchval(
                    "SELECT actual_minutes FROM study_sessions WHERE block_id=$1",
                    block_id,
                ),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE block_id=$1", block_id
                ),
                await conn.fetchval("SELECT status FROM study_blocks WHERE position=1"),
                await conn.fetchval("SELECT state FROM study_days"),
                await conn.fetchval(
                    "SELECT study_minutes FROM topic_progress WHERE topic_id='arrays'"
                ),
            )
        finally:
            await conn.close()

    count, actual, status, pending_status, day_state, study_minutes = asyncio.run(
        persisted()
    )
    assert (count, actual, status, pending_status) == (1, 40, "done", "pending")
    assert day_state == "replan_required"
    assert study_minutes == 40

    stale = client.post(
        "/v1/telegram/updates", headers=headers, json=_payload(102, "done", args)
    )
    assert stale.status_code == 200

    async def session_count():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM study_sessions WHERE block_id=$1", block_id
            )
        finally:
            await conn.close()

    assert asyncio.run(session_count()) == 1


def test_stale_confirmation_rejected_then_fresh_revision_succeeds(block_client):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    selected = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(140, "done", [str(block_id)]),
    )
    assert selected.status_code == 200
    assert selected.json()["status"] == "accepted"

    async def selection_text(operation_id):
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                uuid.UUID(operation_id),
            )
        finally:
            await conn.close()

    assert f"/done {block_id} 1 30" in asyncio.run(
        selection_text(selected.json()["operation_id"])
    )

    async def advance_queue_revision():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE study_days SET queue_revision=2 WHERE study_date=$1",
                datetime.now(ZoneInfo("Asia/Jakarta")).date(),
            )
        finally:
            await conn.close()

    asyncio.run(advance_queue_revision())
    stale_update = _payload(141, "done", [str(block_id), "1", "30"])
    client.post("/v1/telegram/updates", headers=headers, json=stale_update)
    stale = client.post("/v1/telegram/updates", headers=headers, json=stale_update)
    assert stale.status_code == 200
    assert stale.json()["result"]["code"] == "STALE_ACTION"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE block_id=$1", block_id
                ),
                await conn.fetchval("SELECT queue_revision FROM study_days"),
                await conn.fetchval(
                    "SELECT count(*) FROM topic_progress WHERE topic_id='arrays'"
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (0, "pending", 2, 0)

    fresh = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(142, "done", [str(block_id)]),
    )
    assert f"/done {block_id} 2 30" in asyncio.run(
        selection_text(fresh.json()["operation_id"])
    )
    confirm_update = _payload(143, "done", [str(block_id), "2", "25"])
    client.post("/v1/telegram/updates", headers=headers, json=confirm_update)
    confirmed = client.post(
        "/v1/telegram/updates", headers=headers, json=confirm_update
    )
    assert confirmed.json()["result"]["code"] == "BLOCK_DONE"
    assert asyncio.run(persisted()) == (1, "done", 3, 1)


def test_zero_done_closes_block_without_starting_topic(block_client):
    client, block_id, _ = block_client
    response = client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_payload(150, "done", [str(block_id), "1", "0"]),
    )
    assert response.status_code == 200

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT actual_minutes FROM study_sessions WHERE block_id=$1",
                    block_id,
                ),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE block_id=$1", block_id
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM topic_progress WHERE topic_id='arrays'"
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (0, "done", 0)


def test_skip_closes_only_selected_block_without_time_or_refill(block_client):
    client, block_id, untouched_id = block_client
    response = client.post(
        "/v1/telegram/updates",
        headers={"Authorization": "Bearer bot-secret"},
        json=_payload(200, "skip", [str(block_id)]),
    )
    assert response.status_code == 200

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE block_id=$1", block_id
                ),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE block_id=$1", untouched_id
                ),
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval("SELECT state FROM study_days"),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == ("skipped", "pending", 0, "valid")
