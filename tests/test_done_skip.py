import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta
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
                    },
                    {
                        "topic_id": "graphs",
                        "title": "Graphs",
                        "description": "Graphs",
                        "estimate_minutes": 30,
                        "position": 1,
                        "prerequisites": ["arrays"],
                        "resources": [],
                    },
                    {
                        "topic_id": "trees",
                        "title": "Trees",
                        "description": "Trees",
                        "estimate_minutes": 30,
                        "position": 2,
                        "prerequisites": ["graphs"],
                        "resources": [],
                    },
                    {
                        "topic_id": "strings",
                        "title": "Strings",
                        "description": "Strings",
                        "estimate_minutes": 30,
                        "position": 3,
                        "prerequisites": [],
                        "resources": [],
                    },
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


def test_apply_schema_adds_missing_recovery_epoch_column():
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL integration test requires MIGRATION_DATABASE_URL")

    async def migrate_legacy_table():
        await apply_schema(url)
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("ALTER TABLE engine_state DROP COLUMN recovery_epoch")
        finally:
            await conn.close()
        await apply_schema(url)
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchrow(
                "SELECT data_type,is_nullable,column_default FROM information_schema.columns WHERE table_name='engine_state' AND column_name='recovery_epoch'"
            )
        finally:
            await conn.close()

    column = asyncio.run(migrate_legacy_table())
    assert column["data_type"] == "uuid"
    assert column["is_nullable"] == "NO"
    assert "gen_random_uuid" in column["column_default"]


def _confirmation_token(response, command):
    async def text_for_operation():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                uuid.UUID(response.json()["operation_id"]),
            )
        finally:
            await conn.close()

    text = asyncio.run(text_for_operation())
    match = re.search(rf"/{command} ([A-Za-z0-9_-]{{43}})", text)
    assert match, text
    return match.group(1)


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


def test_progress_add_today_keeps_current_plan_available(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    today_date = datetime.now(ZoneInfo("Asia/Jakarta")).date()

    async def expand_budget():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute("UPDATE study_days SET budget_minutes=90")
        finally:
            await conn.close()

    asyncio.run(expand_budget())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(87, "progress", ["add", "arrays", "5", today_date.isoformat()]),
    )
    token = _confirmation_token(preview, "progress confirm")
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(88, "progress", ["confirm", token]),
    )
    assert result.json()["result"]["code"] == "PROGRESS_RECORDED"
    today = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(91, "today", []),
    )
    assert today.json()["result"]["code"] == "PLAN_READY"


def test_progress_add_today_over_capacity_marks_queue_for_replan(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date().isoformat()
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(89, "progress", ["add", "arrays", "15", today]),
    )
    token = _confirmation_token(preview, "progress confirm")
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(90, "progress", ["confirm", token]),
    )
    assert result.json()["result"]["code"] == "REPLAN_REQUIRED"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchrow("SELECT state,queue_revision FROM study_days"),
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval(
                    "SELECT count(*) FROM study_blocks WHERE status='pending'"
                ),
                await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM deliveries WHERE payload->>'text' LIKE '%Use /replan%')"
                ),
            )
        finally:
            await conn.close()

    day, sessions, pending, delivery = asyncio.run(persisted())
    assert (day["state"], day["queue_revision"]) == ("replan_required", 2)
    assert (sessions, pending) == (1, 2)
    assert delivery


def test_progress_correction_today_over_capacity_marks_queue_for_replan(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    session_id = uuid.uuid4()

    async def seed_session():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,'arrays',$2,5)",
                session_id,
                today,
            )
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,study_minutes) VALUES('arrays',5)"
            )
        finally:
            await conn.close()

    asyncio.run(seed_session())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(
            89, "progress", ["edit", str(session_id), today.isoformat(), "40"]
        ),
    )
    token = _confirmation_token(preview, "progress confirm")
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(90, "progress", ["confirm", token]),
    )
    assert result.json()["result"]["code"] == "REPLAN_REQUIRED"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchrow("SELECT state,queue_revision FROM study_days"),
                await conn.fetchval(
                    "SELECT actual_minutes FROM study_sessions WHERE session_id=$1",
                    session_id,
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM study_blocks WHERE status='pending'"
                ),
            )
        finally:
            await conn.close()

    day, minutes, pending = asyncio.run(persisted())
    assert (day["state"], day["queue_revision"]) == ("replan_required", 2)
    assert (minutes, pending) == (40, 2)


def test_moving_today_session_to_past_marks_current_plan_stale(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    yesterday = today - timedelta(days=1)
    session_id = uuid.uuid4()

    async def seed_session():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,'arrays',$2,10)",
                session_id,
                today,
            )
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,study_minutes) VALUES('arrays',10)"
            )
        finally:
            await conn.close()

    asyncio.run(seed_session())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(
            89, "progress", ["edit", str(session_id), yesterday.isoformat(), "30"]
        ),
    )
    token = _confirmation_token(preview, "progress confirm")
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(90, "progress", ["confirm", token]),
    )
    assert result.json()["result"]["code"] == "REPLAN_REQUIRED"

    async def persisted_state():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchrow(
                    "SELECT state,queue_revision FROM study_days WHERE study_date=$1",
                    today,
                ),
                await conn.fetchval(
                    "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                    today,
                ),
                await conn.fetchval(
                    "SELECT COALESCE(sum(actual_minutes),0) FROM study_sessions WHERE study_date=$1",
                    yesterday,
                ),
                await conn.fetchval(
                    "SELECT study_minutes FROM topic_progress WHERE topic_id='arrays'"
                ),
                await conn.fetchval(
                    "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                    uuid.UUID(result.json()["operation_id"]),
                ),
            )
        finally:
            await conn.close()

    day, today_total, yesterday_total, topic_total, text = asyncio.run(
        persisted_state()
    )
    assert (day["state"], day["queue_revision"]) == ("replan_required", 2)
    assert (today_total, yesterday_total, topic_total) == (0, 30, 30)
    assert "historical progress changed" in text
    assert "/replan" in text


def test_past_date_correction_does_not_change_today_queue(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    yesterday = (
        datetime.now(ZoneInfo("Asia/Jakarta"))
        .date()
        .fromordinal(datetime.now(ZoneInfo("Asia/Jakarta")).date().toordinal() - 1)
    )
    session_id = uuid.uuid4()

    async def seed_session():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,'arrays',$2,10)",
                session_id,
                yesterday,
            )
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,study_minutes) VALUES('arrays',10)"
            )
        finally:
            await conn.close()

    asyncio.run(seed_session())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(
            89, "progress", ["edit", str(session_id), yesterday.isoformat(), "30"]
        ),
    )
    token = _confirmation_token(preview, "progress confirm")
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(90, "progress", ["confirm", token]),
    )
    assert result.json()["result"]["code"] == "REPLAN_REQUIRED"

    async def queue_state():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchrow(
                    "SELECT state,queue_revision,budget_minutes FROM study_days WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                ),
                await conn.fetchval(
                    "SELECT actual_minutes FROM study_sessions WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
                ),
                await conn.fetchval(
                    "SELECT actual_minutes FROM study_sessions WHERE session_id=$1",
                    session_id,
                ),
                await conn.fetchval(
                    "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                    uuid.UUID(result.json()["operation_id"]),
                ),
            )
        finally:
            await conn.close()

    day, today_minutes, past_minutes, delivery = asyncio.run(queue_state())
    assert (day["state"], day["queue_revision"]) == ("replan_required", 2)
    assert day["budget_minutes"] == 60
    assert today_minutes is None
    assert past_minutes == 30
    assert "historical progress changed" in delivery
    assert "/replan" in delivery


def test_progress_history_add_and_correction_are_confirmed_and_audited(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    yesterday = (
        datetime.now(ZoneInfo("Asia/Jakarta")).date() - timedelta(days=1)
    ).isoformat()
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(90, "progress", ["add", "arrays", "30", yesterday]),
    )
    assert preview.json()["result"]["code"] == "PROGRESS_CONFIRMATION_REQUIRED"
    token = _confirmation_token(preview, "progress confirm")
    confirmed = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(91, "progress", ["confirm", token]),
    )
    assert confirmed.json()["result"]["code"] == "REPLAN_REQUIRED"
    replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(92, "progress", ["confirm", token]),
    )
    assert replay.json()["result"]["code"] == "REPLAN_REQUIRED"

    async def saved_session():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            row = await conn.fetchrow("SELECT session_id FROM study_sessions")
            return row["session_id"], await conn.fetchval(
                "SELECT count(*) FROM study_session_audit"
            )
        finally:
            await conn.close()

    session_id, audit_count = asyncio.run(saved_session())
    assert audit_count == 1
    new_date = (datetime.now(ZoneInfo("Asia/Jakarta")).date()).isoformat()
    correction = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(93, "progress", ["edit", str(session_id), new_date, "45"]),
    )
    token = _confirmation_token(correction, "progress confirm")
    corrected = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(94, "progress", ["confirm", token]),
    )
    assert corrected.json()["result"]["code"] == "REPLAN_REQUIRED"

    async def audit_history():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            rows = await conn.fetch(
                "SELECT action,before_state,after_state FROM study_session_audit ORDER BY created_at"
            )
            return rows
        finally:
            await conn.close()

    rows = asyncio.run(audit_history())
    assert [row["action"] for row in rows] == ["add", "correct"]
    before_state = json.loads(rows[1]["before_state"])
    after_state = json.loads(rows[1]["after_state"])
    assert before_state["actual_minutes"] == 30
    assert after_state["actual_minutes"] == 45


def test_done_prefills_duration_records_once_and_invalidates_over_budget_queue(
    block_client,
):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    choice = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(100, "done", [str(block_id)]),
    )
    assert choice.status_code == 200
    assert choice.json()["status"] == "completed"

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

    token = _confirmation_token(choice, "done")
    update = _payload(101, "done", [token, "40"])
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

    async def plan_revision():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval("SELECT state_revision FROM plans")
        finally:
            await conn.close()

    assert asyncio.run(plan_revision()) == 0

    stale = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(102, "done", [token, "40"]),
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
    assert selected.json()["status"] == "completed"

    async def selection_text(operation_id):
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                uuid.UUID(operation_id),
            )
        finally:
            await conn.close()

    token = _confirmation_token(selected, "done")
    assert token

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
    stale_update = _payload(141, "done", [token, "30"])
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
    fresh_token = _confirmation_token(fresh, "done")
    confirm_update = _payload(143, "done", [fresh_token, "25"])
    client.post("/v1/telegram/updates", headers=headers, json=confirm_update)
    confirmed = client.post(
        "/v1/telegram/updates", headers=headers, json=confirm_update
    )
    assert confirmed.json()["result"]["code"] == "BLOCK_DONE"
    assert asyncio.run(persisted()) == (1, "done", 3, 1)
    today = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(144, "today", []),
    )
    assert today.json()["result"]["code"] == "PLAN_READY"


def test_zero_done_closes_block_without_starting_topic(block_client):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    response = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(150, "done", [str(block_id)]),
    )
    token = _confirmation_token(response, "done")
    confirmed = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(151, "done", [token, "0"]),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["result"]["code"] == "BLOCK_DONE"

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


@pytest.mark.parametrize(("field", "value"), [("actor_id", 222), ("chat_id", 222)])
def test_consumed_topic_confirmation_checks_identity_before_replay(
    block_client, field, value
):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(290, "complete_topic", ["arrays"]),
    )
    token = _confirmation_token(preview, "complete_topic")
    client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(291, "complete_topic", [token]),
    )

    async def change_identity():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                f"UPDATE action_confirmations SET {field}=$2 WHERE token=$1",
                token,
                value,
            )
        finally:
            await conn.close()

    asyncio.run(change_identity())
    replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(292, "complete_topic", [token]),
    )
    assert replay.json()["result"]["code"] == "STALE_ACTION"


@pytest.mark.parametrize(("field", "value"), [("actor_id", 222), ("chat_id", 222)])
def test_consumed_done_confirmation_checks_identity_before_replay(
    block_client, field, value
):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(293, "done", [str(block_id)]),
    )
    token = _confirmation_token(preview, "done")
    client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(294, "done", [token, "30"]),
    )

    async def change_identity():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                f"UPDATE action_confirmations SET {field}=$2 WHERE token=$1",
                token,
                value,
            )
        finally:
            await conn.close()

    asyncio.run(change_identity())
    replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(295, "done", [token, "30"]),
    )
    assert replay.json()["result"]["code"] == "STALE_ACTION"


def test_complete_topic_requires_confirmation_and_replay_is_idempotent(block_client):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}

    async def seed_progress():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,completed,study_minutes) VALUES('arrays',false,23)"
            )
            await conn.execute(
                "INSERT INTO study_blocks(block_id,plan_id,position,topic_id,planned_minutes) SELECT $1,plan_id,2,'strings',20 FROM plans",
                uuid.uuid4(),
            )
        finally:
            await conn.close()

    asyncio.run(seed_progress())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(300, "complete_topic", ["arrays"]),
    )
    assert preview.json()["result"]["code"] == "TOPIC_CONFIRMATION_REQUIRED"
    token = _confirmation_token(preview, "complete_topic")
    confirm = _payload(301, "complete_topic", [token])
    client.post("/v1/telegram/updates", headers=headers, json=confirm)
    replay = client.post("/v1/telegram/updates", headers=headers, json=confirm)
    assert replay.json()["result"]["code"] == "TOPIC_COMPLETED"
    today_after_completion = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(302, "today", []),
    )
    assert today_after_completion.json()["result"]["code"] == "PLAN_READY"
    second_replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(306, "complete_topic", [token]),
    )
    assert second_replay.json()["result"]["code"] == "TOPIC_COMPLETED"
    reopen_preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(307, "complete_topic", ["arrays"]),
    )
    reopen_token = _confirmation_token(reopen_preview, "complete_topic")
    reopen_confirm = _payload(308, "complete_topic", [reopen_token])
    assert (
        client.post(
            "/v1/telegram/updates", headers=headers, json=reopen_confirm
        ).json()["result"]["code"]
        == "TOPICS_REOPENED"
    )
    assert (
        client.post(
            "/v1/telegram/updates", headers=headers, json=reopen_confirm
        ).json()["result"]["code"]
        == "TOPICS_REOPENED"
    )
    today_after_reopen = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(309, "today", []),
    )
    assert today_after_reopen.json()["result"]["code"] == "PLAN_READY"

    async def revisions():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchrow(
                "SELECT p.state_revision AS plan_revision,s.state_revision AS engine_revision FROM plans p CROSS JOIN engine_state s"
            )
        finally:
            await conn.close()

    revision = asyncio.run(revisions())
    assert revision["plan_revision"] == revision["engine_revision"]

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT completed FROM topic_progress WHERE topic_id='arrays'"
                ),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE block_id=$1", block_id
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM study_blocks WHERE status='pending'"
                ),
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval(
                    "SELECT count(*) FROM deliveries WHERE payload->>'text'='Completed arrays. The queue was not refilled.'"
                ),
                await conn.fetch(
                    "SELECT operation_id,before_state,after_state FROM topic_progress_audit WHERE topic_id='arrays'"
                ),
            )
        finally:
            await conn.close()

    completed, block_status, pending_count, session_count, delivery_count, audit = (
        asyncio.run(persisted())
    )
    assert (completed, block_status, pending_count, session_count, delivery_count) == (
        False,
        "cancelled",
        1,
        0,
        1,
    )
    assert len(audit) == 2
    snapshots = [
        (json.loads(row["before_state"]), json.loads(row["after_state"]))
        for row in audit
    ]
    assert snapshots == [
        (
            {"completed": False, "study_minutes": 23},
            {"completed": True, "study_minutes": 23},
        ),
        (
            {"completed": True, "study_minutes": 23},
            {"completed": False, "study_minutes": 23},
        ),
    ]
    assert len({row["operation_id"] for row in audit}) == 2


def test_replaying_consumed_done_confirmation_does_not_enqueue_delivery(block_client):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(303, "done", [str(block_id)]),
    )
    token = _confirmation_token(preview, "done")
    done = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(304, "done", [token, "30"]),
    )
    replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(305, "done", [token, "30"]),
    )
    assert replay.json()["result"] == done.json()["result"]

    async def delivery_count():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM deliveries WHERE kind='done_skip'"
            )
        finally:
            await conn.close()

    assert asyncio.run(delivery_count()) == 2


def test_topic_action_token_rejects_progress_change_without_revision(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(309, "complete_topic", ["arrays"]),
    )
    token = _confirmation_token(preview, "complete_topic")

    async def change_progress_without_revision():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,completed) VALUES('arrays',true)"
            )
        finally:
            await conn.close()

    asyncio.run(change_progress_without_revision())
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(310, "complete_topic", [token]),
    )
    assert result.json()["result"]["code"] == "STALE_ACTION"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT completed FROM topic_progress WHERE topic_id='arrays'"
                ),
                await conn.fetchval(
                    "SELECT state_revision FROM engine_state WHERE singleton=true"
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (True, 0)


def test_consumed_topic_confirmation_rejects_replay_after_recovery_epoch_changes(
    block_client,
):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(330, "complete_topic", ["arrays"]),
    )
    token = _confirmation_token(preview, "complete_topic")
    confirm = _payload(331, "complete_topic", [token])
    consumed = client.post("/v1/telegram/updates", headers=headers, json=confirm)
    assert consumed.json()["result"]["code"] == "TOPIC_COMPLETED"

    async def change_epoch():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE engine_state SET recovery_epoch=gen_random_uuid() WHERE singleton=true"
            )
        finally:
            await conn.close()

    asyncio.run(change_epoch())
    replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(332, "complete_topic", [token]),
    )
    assert replay.json()["result"]["code"] == "STALE_ACTION"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT count(*) FROM operations WHERE result->'result'->>'code'='TOPIC_COMPLETED'"
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM deliveries WHERE payload->>'text'='Completed arrays. The queue was not refilled.'"
                ),
                await conn.fetchval(
                    "SELECT completed FROM topic_progress WHERE topic_id='arrays'"
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (1, 1, True)


def test_consumed_done_confirmation_rejects_replay_after_recovery_epoch_changes(
    block_client,
):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(333, "done", [str(block_id)]),
    )
    token = _confirmation_token(preview, "done")
    confirmation = _payload(334, "done", [token, "30"])
    consumed = client.post("/v1/telegram/updates", headers=headers, json=confirmation)
    assert consumed.json()["result"]["code"] == "BLOCK_DONE"

    async def change_epoch():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE engine_state SET recovery_epoch=gen_random_uuid() WHERE singleton=true"
            )
        finally:
            await conn.close()

    asyncio.run(change_epoch())
    replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(335, "done", [token, "30"]),
    )
    assert replay.json()["result"]["code"] == "STALE_ACTION"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT count(*) FROM study_sessions WHERE block_id=$1", block_id
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM operations WHERE result->'result'->>'code'='BLOCK_DONE'"
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM deliveries WHERE payload->>'text' LIKE 'Recorded 30 minutes%'"
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (1, 1, 1)


def test_consumed_topic_token_cannot_replay_through_done(block_client):
    client, block_id, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(311, "complete_topic", ["arrays"]),
    )
    token = _confirmation_token(preview, "complete_topic")
    confirmed = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(312, "complete_topic", [token]),
    )
    assert confirmed.json()["result"]["code"] == "TOPIC_COMPLETED"
    replay = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(313, "done", [token, "30"]),
    )
    assert replay.json()["result"]["code"] == "STALE_ACTION"

    async def unchanged():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE block_id=$1", block_id
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(unchanged()) == (0, "cancelled")


def test_topic_confirmation_rejects_stale_queue_revision(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(305, "complete_topic", ["arrays"]),
    )
    assert preview.json()["status"] == "completed"

    async def advance_queue():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute("UPDATE study_days SET queue_revision=2")
        finally:
            await conn.close()

    asyncio.run(advance_queue())
    token = _confirmation_token(preview, "complete_topic")
    confirm = _payload(306, "complete_topic", [token])
    client.post("/v1/telegram/updates", headers=headers, json=confirm)
    result = client.post("/v1/telegram/updates", headers=headers, json=confirm)
    assert result.json()["result"]["code"] == "STALE_ACTION"

    async def still_incomplete():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM topic_progress WHERE topic_id='arrays'"
            )
        finally:
            await conn.close()

    assert asyncio.run(still_incomplete()) == 0


def test_topic_confirmation_rejects_changed_recovery_epoch_without_mutation(
    block_client,
):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(307, "complete_topic", ["arrays"]),
    )
    assert preview.json()["result"]["code"] == "TOPIC_CONFIRMATION_REQUIRED"
    token = _confirmation_token(preview, "complete_topic")

    async def change_epoch():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE engine_state SET recovery_epoch=gen_random_uuid() WHERE singleton=true"
            )
        finally:
            await conn.close()

    asyncio.run(change_epoch())
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(308, "complete_topic", [token]),
    )
    assert result.status_code == 200
    assert result.json()["result"]["code"] == "STALE_ACTION"

    async def unchanged():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval("SELECT count(*) FROM topic_progress"),
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval(
                    "SELECT consumed_at FROM action_confirmations WHERE token=$1", token
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(unchanged()) == (0, 0, None)


def test_concurrent_topic_confirmation_consumes_action_once(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}

    async def record_existing_study_time():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,completed,study_minutes) VALUES('arrays',false,27)"
            )
            await conn.execute(
                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,'arrays',$2,27)",
                uuid.uuid4(),
                datetime.now(ZoneInfo("Asia/Jakarta")).date(),
            )
        finally:
            await conn.close()

    asyncio.run(record_existing_study_time())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(309, "complete_topic", ["arrays"]),
    )
    token = _confirmation_token(preview, "complete_topic")
    updates = [
        _payload(update_id, "complete_topic", [token]) for update_id in (310, 311)
    ]

    async def concurrently_confirm():
        return await asyncio.gather(
            *(
                asyncio.to_thread(
                    client.post,
                    "/v1/telegram/updates",
                    headers=headers,
                    json=update,
                )
                for update in updates
            )
        )

    first, second = asyncio.run(concurrently_confirm())
    if first.status_code == 409:
        first = client.post(
            "/v1/telegram/updates",
            headers=headers,
            json=_payload(312, "complete_topic", [token]),
        )
    if second.status_code == 409:
        second = client.post(
            "/v1/telegram/updates",
            headers=headers,
            json=_payload(313, "complete_topic", [token]),
        )
    assert first.status_code == second.status_code == 200
    assert first.json()["result"] == second.json()["result"]
    assert first.json()["result"]["code"] == "TOPIC_COMPLETED"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval(
                    "SELECT completed FROM topic_progress WHERE topic_id='arrays'"
                ),
                await conn.fetchval(
                    "SELECT study_minutes FROM topic_progress WHERE topic_id='arrays'"
                ),
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
                await conn.fetchval(
                    "SELECT count(*) FROM operations WHERE result->'result'->>'code'='TOPIC_COMPLETED'"
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM deliveries WHERE logical_key LIKE 'operation:%:topic' AND payload->>'text' = 'Completed arrays. The queue was not refilled.'"
                ),
                await conn.fetchrow(
                    "SELECT consumed_at,result_code,result_text FROM action_confirmations WHERE token=$1",
                    token,
                ),
            )
        finally:
            await conn.close()

    completed, minutes, sessions, results, deliveries, confirmation = asyncio.run(
        persisted()
    )
    assert completed is True
    assert minutes == 27
    assert sessions == 1
    assert results == 2
    assert deliveries == 1
    assert confirmation["consumed_at"] is not None
    assert confirmation["result_code"] == "TOPIC_COMPLETED"
    assert confirmation["result_text"]


def test_confirmation_binding_and_expiry_reject_without_mutation(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(320, "complete_topic", ["arrays"]),
    )
    token = _confirmation_token(preview, "complete_topic")

    async def change_actor():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE action_confirmations SET actor_id=222 WHERE token=$1", token
            )
        finally:
            await conn.close()

    asyncio.run(change_actor())
    # An actor mismatch is rejected; the record remains unconsumed.
    mismatch = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(321, "complete_topic", [token]),
    )
    assert mismatch.json()["result"]["code"] == "STALE_ACTION"

    async def expiration():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE action_confirmations SET actor_id=111,expires_at=now()-interval '1 second' WHERE token=$1",
                token,
            )
        finally:
            await conn.close()

    asyncio.run(expiration())
    expired = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(322, "complete_topic", [token]),
    )
    assert expired.json()["result"]["code"] == "STALE_ACTION"

    async def unmodified():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval("SELECT count(*) FROM topic_progress"),
                await conn.fetchval(
                    "SELECT consumed_at FROM action_confirmations WHERE token=$1", token
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(unmodified()) == (0, None)


def test_reopening_prerequisite_reopens_dependents_preserving_minutes(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}

    async def setup_progress():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,completed,study_minutes) VALUES('arrays',true,15),('graphs',true,20),('trees',true,25)"
            )
            plan_id = await conn.fetchval("SELECT current_plan_id FROM study_days")
            await conn.execute(
                "INSERT INTO study_blocks(block_id,plan_id,position,topic_id,planned_minutes) VALUES($1,$2,2,'strings',30)",
                uuid.uuid4(),
                plan_id,
            )
            await conn.execute(
                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,'graphs',$2,20),($3,'trees',$2,25)",
                uuid.uuid4(),
                datetime.now(ZoneInfo("Asia/Jakarta")).date(),
                uuid.uuid4(),
            )
        finally:
            await conn.close()

    asyncio.run(setup_progress())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(310, "complete_topic", ["arrays"]),
    )
    assert preview.json()["result"]["code"] == "REOPEN_CONFIRMATION_REQUIRED"
    token = _confirmation_token(preview, "complete_topic")
    confirm = _payload(311, "complete_topic", [token])
    client.post("/v1/telegram/updates", headers=headers, json=confirm)
    result = client.post("/v1/telegram/updates", headers=headers, json=confirm)
    assert result.json()["result"]["code"] == "TOPICS_REOPENED"

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetch(
                    "SELECT topic_id,completed,study_minutes FROM topic_progress ORDER BY topic_id"
                ),
                await conn.fetchval("SELECT sum(actual_minutes) FROM study_sessions"),
                await conn.fetchval("SELECT state FROM study_days"),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE topic_id='strings'"
                ),
            )
        finally:
            await conn.close()

    progress, minutes, state, unaffected_block_status = asyncio.run(persisted())
    assert [(r["topic_id"], r["completed"], r["study_minutes"]) for r in progress] == [
        ("arrays", False, 15),
        ("graphs", False, 20),
        ("trees", False, 25),
    ]
    assert (minutes, state, unaffected_block_status) == (45, "valid", "pending")


def test_progress_reopen_audits_topic_and_dependents_without_changing_time(
    block_client,
):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}

    async def setup_progress():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,completed,study_minutes) VALUES('arrays',true,15),('graphs',true,20),('trees',true,25)"
            )
        finally:
            await conn.close()

    asyncio.run(setup_progress())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(314, "progress", ["reopen", "arrays"]),
    )
    assert preview.json()["result"]["code"] == "PROGRESS_CONFIRMATION_REQUIRED"
    token = _confirmation_token(preview, "progress confirm")
    result = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(315, "progress", ["confirm", token]),
    )
    assert result.json()["result"]["code"] == "TOPICS_REOPENED"

    async def audited_progress():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetch(
                    "SELECT topic_id,completed,study_minutes FROM topic_progress ORDER BY topic_id"
                ),
                await conn.fetch(
                    "SELECT operation_id,topic_id,before_state,after_state FROM topic_progress_audit ORDER BY topic_id"
                ),
                await conn.fetchval("SELECT count(*) FROM study_sessions"),
            )
        finally:
            await conn.close()

    progress, audit, session_count = asyncio.run(audited_progress())
    assert [(r["topic_id"], r["completed"], r["study_minutes"]) for r in progress] == [
        ("arrays", False, 15),
        ("graphs", False, 20),
        ("trees", False, 25),
    ]
    assert session_count == 0
    assert [row["topic_id"] for row in audit] == ["arrays", "graphs", "trees"]
    assert len({row["operation_id"] for row in audit}) == 1
    for row in audit:
        assert json.loads(row["before_state"]) == {
            "completed": True,
            "study_minutes": {"arrays": 15, "graphs": 20, "trees": 25}[row["topic_id"]],
        }
        assert json.loads(row["after_state"]) == {
            "completed": False,
            "study_minutes": {"arrays": 15, "graphs": 20, "trees": 25}[row["topic_id"]],
        }


def test_reopening_prerequisite_invalidates_pending_dependent_block(block_client):
    client, _, _ = block_client
    headers = {"Authorization": "Bearer bot-secret"}

    async def setup_dependent_block():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO topic_progress(topic_id,completed) VALUES('arrays',true),('graphs',true),('trees',true)"
            )
            plan_id = await conn.fetchval("SELECT current_plan_id FROM study_days")
            await conn.execute(
                "INSERT INTO study_blocks(block_id,plan_id,position,topic_id,planned_minutes) VALUES($1,$2,2,'trees',30)",
                uuid.uuid4(),
                plan_id,
            )
        finally:
            await conn.close()

    asyncio.run(setup_dependent_block())
    preview = client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(312, "complete_topic", ["arrays"]),
    )
    token = _confirmation_token(preview, "complete_topic")
    client.post(
        "/v1/telegram/updates",
        headers=headers,
        json=_payload(313, "complete_topic", [token]),
    )

    async def persisted():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return (
                await conn.fetchval("SELECT state FROM study_days"),
                await conn.fetchval(
                    "SELECT status FROM study_blocks WHERE topic_id='trees'"
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == ("replan_required", "cancelled")
