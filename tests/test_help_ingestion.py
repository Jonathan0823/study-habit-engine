import asyncio
import json
import os
import subprocess
import sys
import uuid
from itertools import count

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.commands import COMMANDS, help_text
from app.db import apply_schema
from app.main import app

_update_ids = count(uuid.uuid4().int % (2**62) + 1)


def next_update_id() -> str:
    return str(next(_update_ids))


@pytest.fixture
def client():
    database_url = os.environ["MIGRATION_DATABASE_URL"]
    os.environ["ADMIN_SERVICE_TOKEN"] = "admin-secret"

    async def reset_database():
        await apply_schema(database_url)
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(
                "TRUNCATE admin_activation_operations, curriculum_previews, curriculum_versions, engine_state, deliveries, telegram_ingestion, operations, telegram_cursor CASCADE"
            )
            await conn.execute("INSERT INTO engine_state(singleton) VALUES(true)")
        finally:
            await conn.close()

    asyncio.run(reset_database())
    with TestClient(app) as test_client:
        yield test_client


def update(
    update_id: str,
    *,
    command: str = "help",
    actor_id: str = "111",
    chat_id: str = "111",
) -> dict:
    return {
        "bot_id": "12345",
        "update_id": update_id,
        "actor_id": actor_id,
        "chat_id": chat_id,
        "chat_type": "private",
        "poller_fence": 1,
        "payload": {"kind": "command", "command": command, "arguments": []},
    }


def poller(client):
    response = client.post(
        "/v1/telegram/claim",
        json={"bot_id": "12345", "owner_id": str(uuid.uuid4())},
        headers={"Authorization": "Bearer bot-secret"},
    )
    assert response.status_code == 200
    return response.json()["fence"]


def test_help_is_admitted_idempotently_and_outboxed(client):
    headers = {"Authorization": "Bearer bot-secret"}
    body = update(next_update_id())
    body["poller_fence"] = poller(client)
    first = client.post("/v1/telegram/updates", json=body, headers=headers)
    duplicate = client.post("/v1/telegram/updates", json=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert duplicate.json() == first.json()

    async def persisted():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            deliveries = await conn.fetch(
                "SELECT operation_id, payload, status FROM deliveries"
            )
            operations = await conn.fetchval("SELECT count(*) FROM operations")
            next_offset = await conn.fetchval(
                "SELECT next_offset FROM telegram_cursor WHERE bot_id=12345"
            )
            return deliveries, operations, next_offset
        finally:
            await conn.close()

    deliveries, operations, next_offset = asyncio.run(persisted())
    assert operations == 1
    assert next_offset == int(body["update_id"]) + 1
    assert len(deliveries) == 1
    assert deliveries[0]["operation_id"] == uuid.UUID(first.json()["operation_id"])
    assert json.loads(deliveries[0]["payload"])["text"] == help_text()
    assert deliveries[0]["status"] == "pending"


def test_budget_update_is_bounded_idempotent_and_replans(client):
    headers = {"Authorization": "Bearer bot-secret"}
    fence = poller(client)
    body = update(next_update_id(), command="budget")
    body["poller_fence"] = fence
    body["payload"]["arguments"] = ["0"]
    accepted = client.post("/v1/telegram/updates", json=body, headers=headers)
    replay = client.post("/v1/telegram/updates", json=body, headers=headers)
    assert accepted.json()["status"] == "accepted"
    assert replay.json() == accepted.json()

    invalid = update(next_update_id(), command="budget")
    invalid["poller_fence"] = fence
    invalid["payload"]["arguments"] = ["91"]
    rejected = client.post("/v1/telegram/updates", json=invalid, headers=headers)
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["result"]["code"] == "INVALID_BUDGET"

    async def persisted():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            day = await conn.fetchrow(
                "SELECT budget_minutes,requested_generation,state FROM study_days WHERE study_date=(now() AT TIME ZONE 'Asia/Jakarta')::date"
            )
            return day, await conn.fetchval("SELECT count(*) FROM planning_jobs")
        finally:
            await conn.close()

    day, jobs = asyncio.run(persisted())
    assert day["budget_minutes"] == 0
    assert day["requested_generation"] == 1
    assert day["state"] == "replan_required"
    assert jobs == 1


def test_curriculum_module_imports_independently():
    subprocess.run([sys.executable, "-c", "import app.curriculum"], check=True)


def test_shared_help_explains_confirmation_and_topic_selection():
    text = help_text()
    assert (
        "/done [block_id] | <token> <actual_minutes> - Select a pending block by ID, then confirm with the displayed token and actual minutes"
        in text
    )
    assert (
        "/complete_topic [topic_id] - Choose from listed topics when omitted; preview prerequisites and queue impact, then confirm completion"
        in text
    )


def test_help_and_ingestion_share_progress_command(client):
    assert any(command[0] == "progress" for command in COMMANDS)
    assert (
        "/progress [YYYY-MM-DD | add TOPIC MINUTES [YYYY-MM-DD] | edit SESSION_ID YYYY-MM-DD MINUTES | reopen TOPIC_ID | confirm TOKEN]"
        in help_text()
    )


def test_all_shared_future_commands_are_recognized_but_not_implemented(client):
    headers = {"Authorization": "Bearer bot-secret"}
    fence = poller(client)
    future_commands = [
        name
        for name, _, _ in COMMANDS
        if name
        not in (
            "help",
            "today",
            "replan",
            "budget",
            "done",
            "skip",
            "complete_topic",
            "progress",
            "learn",
            "approve",
            "reject",
        )
    ]

    for command in future_commands:
        body = update(next_update_id(), command=command)
        body["poller_fence"] = fence
        response = client.post("/v1/telegram/updates", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        assert response.json()["result"]["code"] == "NOT_IMPLEMENTED"
        assert response.json()["callback_notice"] is None

    body = update(next_update_id(), command="not_a_command")
    body["poller_fence"] = fence
    unsupported = client.post("/v1/telegram/updates", json=body, headers=headers)
    assert unsupported.status_code == 200
    assert unsupported.json()["result"]["code"] == "UNSUPPORTED_COMMAND"

    today_body = update(next_update_id(), command="today")
    today_body["poller_fence"] = fence
    accepted_today = client.post(
        "/v1/telegram/updates", json=today_body, headers=headers
    )
    assert accepted_today.status_code == 200
    assert accepted_today.json()["status"] == "accepted"
    assert accepted_today.json()["result"]["code"] == "PLAN_ACCEPTED"

    replan_body = update(next_update_id(), command="replan")
    replan_body["poller_fence"] = fence
    accepted_replan = client.post(
        "/v1/telegram/updates", json=replan_body, headers=headers
    )
    assert accepted_replan.status_code == 200
    assert accepted_replan.json()["status"] == "accepted"
    assert accepted_replan.json()["result"]["code"] == "PLAN_ACCEPTED"

    async def persisted():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return (
                await conn.fetchval("SELECT count(*) FROM operations"),
                await conn.fetchval("SELECT count(*) FROM deliveries"),
                await conn.fetchval("SELECT count(*) FROM planning_jobs"),
                await conn.fetchval(
                    "SELECT count(*) FROM planning_jobs WHERE status='completed'"
                ),
                await conn.fetchval(
                    "SELECT count(*) FROM planning_jobs WHERE status='pending'"
                ),
            )
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (len(future_commands) + 3, 0, 2, 1, 1)


def test_update_id_with_different_content_conflicts(client):
    fence = poller(client)
    body = update(next_update_id())
    body["poller_fence"] = fence
    headers = {"Authorization": "Bearer bot-secret"}
    assert (
        client.post("/v1/telegram/updates", json=body, headers=headers).status_code
        == 200
    )
    body["payload"]["arguments"] = ["changed"]
    assert (
        client.post("/v1/telegram/updates", json=body, headers=headers).status_code
        == 409
    )


def test_unauthorized_update_is_disposed_without_help_or_learning_state(client):
    body = update(next_update_id(), actor_id="222", chat_id="222")
    body["poller_fence"] = poller(client)
    response = client.post(
        "/v1/telegram/updates",
        json=body,
        headers={"Authorization": "Bearer bot-secret"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["callback_notice"] is None

    async def persisted():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM operations"
            ), await conn.fetchval("SELECT count(*) FROM deliveries")
        finally:
            await conn.close()

    operations, deliveries = asyncio.run(persisted())
    assert operations == 1
    assert deliveries == 0


def test_group_is_rejected_without_creating_delivery(client):
    body = update(next_update_id())
    body["poller_fence"] = poller(client)
    body["chat_type"] = "group"
    response = client.post(
        "/v1/telegram/updates",
        json=body,
        headers={"Authorization": "Bearer bot-secret"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_delivery_retry_reuses_the_original_operation(client):
    headers = {"Authorization": "Bearer bot-secret"}
    body = update(next_update_id())
    body["poller_fence"] = poller(client)
    accepted = client.post("/v1/telegram/updates", json=body, headers=headers).json()
    delivery = client.post("/v1/deliveries/claim", headers=headers).json()
    failed = client.post(
        f"/v1/deliveries/{delivery['delivery_id']}/result",
        json={"fence": delivery["fence"], "outcome": "transient_failure"},
        headers=headers,
    )
    assert failed.status_code == 200
    assert client.post("/v1/deliveries/claim", headers=headers).status_code == 204

    async def make_retry_due():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE deliveries SET next_attempt_at=now() WHERE delivery_id=$1",
                uuid.UUID(delivery["delivery_id"]),
            )
        finally:
            await conn.close()

    asyncio.run(make_retry_due())
    retried_delivery = client.post("/v1/deliveries/claim", headers=headers).json()
    assert retried_delivery["delivery_id"] == delivery["delivery_id"]
    assert retried_delivery["operation_id"] == accepted["operation_id"]

    async def persisted_counts():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM operations"
            ), await conn.fetchval("SELECT count(*) FROM deliveries")
        finally:
            await conn.close()

    assert asyncio.run(persisted_counts()) == (1, 1)


def admin_idempotency_headers(headers):
    return {**headers, "Idempotency-Key": str(uuid.uuid4())}


def test_curriculum_import_preview_and_activate_exact_version(client):
    headers = {"Authorization": "Bearer admin-secret"}
    content = {
        "topics": [
            {
                "topic_id": "arrays",
                "title": "Arrays",
                "description": "Understand indexed collections.",
                "estimate_minutes": 45,
                "position": 0,
                "prerequisites": [],
                "resources": [
                    {"url": "https://example.org/arrays", "title": "Arrays guide"}
                ],
            }
        ]
    }
    imported = client.post(
        "/v1/admin/curricula/import",
        json=content,
        headers=admin_idempotency_headers(headers),
    )
    assert imported.status_code == 200
    version = imported.json()["version"]
    assert imported.json()["status"] == "draft"
    preview = client.post(
        f"/v1/admin/curricula/{version}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    )
    assert preview.status_code == 200
    token = preview.json()["confirmation_token"]
    activated = client.post(
        f"/v1/admin/confirmations/{token}",
        json={},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "approved"
    active = client.get("/v1/admin/curricula/active", headers=headers).json()["active"]
    assert active["version"] == version
    assert active["content_hash"] == imported.json()["content_hash"]
    assert active["curriculum"]["topics"][0]["topic_id"] == "arrays"
    duplicate = client.post(
        f"/v1/admin/confirmations/{token}",
        json={},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert duplicate.json() == activated.json()


def test_revision_mapping_previews_and_carries_completion_without_moving_minutes(
    client,
):
    headers = {"Authorization": "Bearer admin-secret"}
    original = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum("old"),
        headers=admin_idempotency_headers(headers),
    ).json()
    first_preview = client.post(
        f"/v1/admin/curricula/{original['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()
    assert (
        client.post(
            f"/v1/admin/confirmations/{first_preview['confirmation_token']}",
            json={},
            headers=admin_idempotency_headers(headers),
        ).status_code
        == 200
    )

    async def seed_history():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE topic_progress SET completed=true,study_minutes=37 WHERE topic_id='old'"
            )
            await conn.execute(
                "INSERT INTO study_sessions(session_id,topic_id,study_date,actual_minutes) VALUES($1,'old',CURRENT_DATE,37)",
                uuid.uuid4(),
            )
            await conn.execute(
                "INSERT INTO study_days(study_date,state,queue_revision) VALUES(CURRENT_DATE,'valid',4)"
            )
        finally:
            await conn.close()

    asyncio.run(seed_history())
    revised = {
        "topics": [
            {
                **valid_curriculum("new")["topics"][0],
                "description": "Revised objective",
                "position": 0,
            },
        ]
    }
    imported = client.post(
        "/v1/admin/curricula/import",
        json=revised,
        headers=admin_idempotency_headers(headers),
    ).json()
    mapping = [
        {"from_topic_ids": ["old"], "to_topic_id": "new", "carry_completion": True}
    ]
    preview = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": mapping},
        headers=admin_idempotency_headers(headers),
    )
    assert preview.status_code == 200
    assert preview.json()["removed_topic_ids"] == ["old"]
    assert preview.json()["new_topic_ids"] == ["new"]
    assert preview.json()["completion_carryover_topic_ids"] == ["new"]
    assert preview.json()["queue_impact"][0]["will_invalidate"] is True
    activated = client.post(
        f"/v1/admin/confirmations/{preview.json()['confirmation_token']}",
        json={},
        headers=admin_idempotency_headers(headers),
    )
    assert activated.status_code == 200

    async def verify_history():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            source = await conn.fetchrow(
                "SELECT completed,study_minutes FROM topic_progress WHERE topic_id='old'"
            )
            target = await conn.fetchrow(
                "SELECT completed,study_minutes FROM topic_progress WHERE topic_id='new'"
            )
            total = await conn.fetchval(
                "SELECT sum(actual_minutes) FROM study_sessions WHERE topic_id='old'"
            )
            queue = await conn.fetchrow(
                "SELECT state,queue_revision FROM study_days WHERE study_date=CURRENT_DATE"
            )
            return source, target, total, queue
        finally:
            await conn.close()

    source, target, total, queue = asyncio.run(verify_history())
    assert tuple(source.values()) == (True, 37)
    assert tuple(target.values()) == (True, 0)
    assert total == 37
    assert tuple(queue.values()) == ("replan_required", 5)


def test_transitive_prerequisite_invalidation_matches_activation(client):
    headers = {"Authorization": "Bearer admin-secret"}
    old_topics = [
        {**valid_curriculum("root")["topics"][0], "position": 0},
        {
            **valid_curriculum("b")["topics"][0],
            "position": 1,
            "prerequisites": ["root"],
        },
        {**valid_curriculum("c")["topics"][0], "position": 2, "prerequisites": ["b"]},
        {**valid_curriculum("d")["topics"][0], "position": 3, "prerequisites": ["c"]},
    ]
    original = client.post(
        "/v1/admin/curricula/import",
        json={"topics": old_topics},
        headers=admin_idempotency_headers(headers),
    ).json()
    first_preview = client.post(
        f"/v1/admin/curricula/{original['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()
    assert (
        client.post(
            f"/v1/admin/confirmations/{first_preview['confirmation_token']}",
            json={},
            headers=admin_idempotency_headers(headers),
        ).status_code
        == 200
    )

    async def mark_chain_complete():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE topic_progress SET completed=true,study_minutes=11"
            )
        finally:
            await conn.close()

    asyncio.run(mark_chain_complete())
    new_topics = [
        {**old_topics[0], "prerequisites": ["new-root"]},
        *old_topics[1:],
        {**valid_curriculum("new-root")["topics"][0], "position": 4},
    ]
    revised = client.post(
        "/v1/admin/curricula/import",
        json={"topics": new_topics},
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{revised['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    )
    assert preview.status_code == 200
    invalidated = preview.json()["prerequisite_invalidated_topic_ids"]
    assert invalidated == ["b", "c", "d", "root"]
    activation = client.post(
        f"/v1/admin/confirmations/{preview.json()['confirmation_token']}",
        json={},
        headers=admin_idempotency_headers(headers),
    )
    assert activation.status_code == 200

    async def read_completion():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            rows = await conn.fetch(
                "SELECT topic_id,completed,study_minutes FROM topic_progress WHERE topic_id = ANY($1::text[])",
                invalidated,
            )
            return {
                row["topic_id"]: (row["completed"], row["study_minutes"])
                for row in rows
            }
        finally:
            await conn.close()

    after = asyncio.run(read_completion())
    assert set(after) == set(invalidated)
    assert all(
        completed is False and minutes == 11 for completed, minutes in after.values()
    )


def test_changed_objective_mapping_cannot_retain_source_identity_or_minutes(client):
    headers = {"Authorization": "Bearer admin-secret"}
    original = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum("same"),
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{original['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()
    assert (
        client.post(
            f"/v1/admin/confirmations/{preview['confirmation_token']}",
            json={},
            headers=admin_idempotency_headers(headers),
        ).status_code
        == 200
    )

    async def seed_minutes():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE topic_progress SET study_minutes=23 WHERE topic_id='same'"
            )
        finally:
            await conn.close()

    asyncio.run(seed_minutes())
    revised_content = {
        "topics": [
            {
                **valid_curriculum("same")["topics"][0],
                "description": "A changed objective",
                "position": 0,
            },
            {**valid_curriculum("target")["topics"][0], "position": 1},
        ]
    }
    revised = client.post(
        "/v1/admin/curricula/import",
        json=revised_content,
        headers=admin_idempotency_headers(headers),
    ).json()
    rejected = client.post(
        f"/v1/admin/curricula/{revised['version']}/preview",
        json={
            "mapping": [
                {
                    "from_topic_ids": ["same"],
                    "to_topic_id": "target",
                    "carry_completion": False,
                }
            ]
        },
        headers=admin_idempotency_headers(headers),
    )
    assert rejected.status_code == 422
    assert "must be retired" in rejected.json()["detail"]["message"]

    async def verify_source_history():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT study_minutes FROM topic_progress WHERE topic_id='same'"
            )
        finally:
            await conn.close()

    assert asyncio.run(verify_source_history()) == 23


def test_non_valid_queue_revision_change_stales_preview(client):
    headers = {"Authorization": "Bearer admin-secret"}

    async def seed_non_valid_queue():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO study_days(study_date,state,queue_revision) VALUES(CURRENT_DATE - 1,'replan_required',2)"
            )
        finally:
            await conn.close()

    asyncio.run(seed_non_valid_queue())
    imported = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum("queue"),
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    )
    assert preview.status_code == 200
    assert preview.json()["queue_impact"][0]["state"] == "replan_required"
    assert preview.json()["queue_impact"][0]["will_invalidate"] is False

    async def revise_queue():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE study_days SET queue_revision=3 WHERE study_date=CURRENT_DATE - 1"
            )
        finally:
            await conn.close()

    asyncio.run(revise_queue())
    activated = client.post(
        f"/v1/admin/confirmations/{preview.json()['confirmation_token']}",
        json={},
        headers=admin_idempotency_headers(headers),
    )
    assert activated.status_code == 409
    assert activated.json()["detail"]["code"] == "STALE_PREVIEW"


def valid_curriculum(topic_id="one"):
    return {
        "topics": [
            {
                "topic_id": topic_id,
                "title": "One",
                "description": "First topic",
                "estimate_minutes": 15,
                "position": 0,
                "prerequisites": [],
                "resources": [{"url": "https://example.org/one", "title": "One"}],
            }
        ]
    }


def invalid_curricula():
    missing_fields = valid_curriculum()
    del missing_fields["topics"][0]["description"]
    missing_prerequisites_field = valid_curriculum()
    del missing_prerequisites_field["topics"][0]["prerequisites"]
    invalid_estimate = valid_curriculum()
    invalid_estimate["topics"][0]["estimate_minutes"] = 0
    missing_resources = valid_curriculum()
    del missing_resources["topics"][0]["resources"]
    empty_resources = valid_curriculum()
    empty_resources["topics"][0]["resources"] = []
    invalid_topic_id = valid_curriculum("bad/id")
    duplicate_ids = valid_curriculum()
    duplicate_ids["topics"].append({**duplicate_ids["topics"][0], "position": 1})
    duplicate_order = valid_curriculum()
    duplicate_order["topics"].append(
        {**duplicate_order["topics"][0], "topic_id": "two"}
    )
    missing_prerequisite = valid_curriculum()
    missing_prerequisite["topics"][0]["prerequisites"] = ["unknown"]
    cycle = {
        "topics": [
            {
                **valid_curriculum()["topics"][0],
                "topic_id": "a",
                "prerequisites": ["b"],
            },
            {
                **valid_curriculum()["topics"][0],
                "topic_id": "b",
                "position": 1,
                "prerequisites": ["a"],
            },
        ]
    }
    invalid_url = valid_curriculum()
    invalid_url["topics"][0]["resources"][0]["url"] = "file:///tmp/x"
    whitespace_topic_title = valid_curriculum()
    whitespace_topic_title["topics"][0]["title"] = " \t\n"
    whitespace_topic_description = valid_curriculum()
    whitespace_topic_description["topics"][0]["description"] = " \t\n"
    whitespace_resource_title = valid_curriculum()
    whitespace_resource_title["topics"][0]["resources"][0]["title"] = " \t\n"
    return [
        ("missing fields", missing_fields),
        ("missing prerequisites field", missing_prerequisites_field),
        ("zero estimate", invalid_estimate),
        ("missing resources", missing_resources),
        ("empty resources", empty_resources),
        ("invalid topic ID", invalid_topic_id),
        ("duplicate topic ID", duplicate_ids),
        ("duplicate order", duplicate_order),
        ("missing prerequisite", missing_prerequisite),
        ("cycle", cycle),
        ("invalid URL", invalid_url),
        ("whitespace topic title", whitespace_topic_title),
        ("whitespace topic description", whitespace_topic_description),
        ("whitespace resource title", whitespace_resource_title),
    ]


@pytest.mark.parametrize(
    "case,invalid",
    invalid_curricula(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_invalid_curriculum_import_preserves_active_version(client, case, invalid):
    headers = {"Authorization": "Bearer admin-secret"}
    imported = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()
    key = str(uuid.uuid4())
    activated = client.post(
        f"/v1/admin/confirmations/{preview['confirmation_token']}",
        json={},
        headers={**headers, "Idempotency-Key": key},
    )
    assert activated.status_code == 200
    active_before = client.get("/v1/admin/curricula/active", headers=headers).json()

    rejected = client.post(
        "/v1/admin/curricula/import",
        json=invalid,
        headers=admin_idempotency_headers(headers),
    )
    assert rejected.status_code == 422, case
    assert (
        client.get("/v1/admin/curricula/active", headers=headers).json()
        == active_before
    )


def test_stale_preview_requires_fresh_preview_and_fresh_preview_activates(client):
    headers = {"Authorization": "Bearer admin-secret"}
    imported = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()

    async def change_revision():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE engine_state SET progress_revision=progress_revision+1 WHERE singleton=true"
            )
        finally:
            await conn.close()

    asyncio.run(change_revision())

    stale = client.post(
        f"/v1/admin/confirmations/{preview['confirmation_token']}",
        json={},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_PREVIEW"
    assert client.get("/v1/admin/curricula/active", headers=headers).json() == {
        "active": None
    }

    fresh = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    )
    assert fresh.status_code == 200
    activated = client.post(
        f"/v1/admin/confirmations/{fresh.json()['confirmation_token']}",
        json={},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert activated.status_code == 200
    assert (
        client.get("/v1/admin/curricula/active", headers=headers).json()["active"][
            "version"
        ]
        == imported["version"]
    )


def test_curriculum_content_and_hash_are_immutable_in_database(client):
    headers = {"Authorization": "Bearer admin-secret"}
    imported = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers=admin_idempotency_headers(headers),
    ).json()

    async def mutate():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "UPDATE curriculum_versions SET content_hash='tampered' WHERE curriculum_id=$1",
                    uuid.UUID(imported["version"]),
                )
        finally:
            await conn.close()

    asyncio.run(mutate())
    validated = client.post(
        f"/v1/admin/curricula/{imported['version']}/validate", headers=headers
    )
    assert validated.status_code == 200
    assert validated.json()["content_hash"] == imported["content_hash"]


def test_active_curriculum_checks_stored_hash(client):
    headers = {"Authorization": "Bearer admin-secret"}
    imported = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()
    activated = client.post(
        f"/v1/admin/confirmations/{preview['confirmation_token']}",
        json={},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert activated.status_code == 200

    async def tamper():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "ALTER TABLE curriculum_versions DISABLE TRIGGER curriculum_content_immutable"
            )
            await conn.execute(
                "UPDATE curriculum_versions SET content_hash='tampered' WHERE curriculum_id=$1",
                uuid.UUID(imported["version"]),
            )
            await conn.execute(
                "ALTER TABLE curriculum_versions ENABLE TRIGGER curriculum_content_immutable"
            )
        finally:
            await conn.close()

    asyncio.run(tamper())

    response = client.get("/v1/admin/curricula/active", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CURRICULUM_HASH_MISMATCH"


def test_stored_hash_is_checked_by_validate_preview_and_activate(client):
    headers = {"Authorization": "Bearer admin-secret"}
    imported = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()

    async def tamper():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "ALTER TABLE curriculum_versions DISABLE TRIGGER curriculum_content_immutable"
            )
            await conn.execute(
                "UPDATE curriculum_versions SET content_hash='tampered' WHERE curriculum_id=$1",
                uuid.UUID(imported["version"]),
            )
            await conn.execute(
                "ALTER TABLE curriculum_versions ENABLE TRIGGER curriculum_content_immutable"
            )
        finally:
            await conn.close()

    asyncio.run(tamper())

    assert (
        client.post(
            f"/v1/admin/curricula/{imported['version']}/validate", headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/admin/curricula/{imported['version']}/preview",
            json={"mapping": []},
            headers=admin_idempotency_headers(headers),
        ).status_code
        == 409
    )
    activation = client.post(
        f"/v1/admin/confirmations/{preview['confirmation_token']}",
        json={},
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert activation.status_code == 409
    assert client.get("/v1/admin/curricula/active", headers=headers).json() == {
        "active": None
    }


def test_import_and_preview_idempotency_replay_and_conflict(client):
    headers = {"Authorization": "Bearer admin-secret"}
    import_key = str(uuid.uuid4())
    first_import = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers={**headers, "Idempotency-Key": import_key},
    )
    replay_import = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers={**headers, "Idempotency-Key": import_key},
    )
    assert replay_import.json() == first_import.json()
    conflict_import = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum("different"),
        headers={**headers, "Idempotency-Key": import_key},
    )
    assert conflict_import.status_code == 409
    method_conflict = client.post(
        f"/v1/admin/curricula/{first_import.json()['version']}/preview",
        json={"mapping": []},
        headers={**headers, "Idempotency-Key": import_key},
    )
    assert method_conflict.status_code == 409

    version = first_import.json()["version"]
    preview_key = str(uuid.uuid4())
    preview_url = f"/v1/admin/curricula/{version}/preview"
    first_preview = client.post(
        preview_url,
        json={"mapping": []},
        headers={**headers, "Idempotency-Key": preview_key},
    )
    replay_preview = client.post(
        preview_url,
        json={"mapping": []},
        headers={**headers, "Idempotency-Key": preview_key},
    )
    assert replay_preview.json() == first_preview.json()
    conflict_preview = client.post(
        f"/v1/admin/curricula/{uuid.uuid4()}/preview",
        json={"mapping": []},
        headers={**headers, "Idempotency-Key": preview_key},
    )
    assert conflict_preview.status_code == 409


def test_activation_requires_and_replays_idempotency_key(client):
    headers = {"Authorization": "Bearer admin-secret"}
    imported = client.post(
        "/v1/admin/curricula/import",
        json=valid_curriculum(),
        headers=admin_idempotency_headers(headers),
    ).json()
    preview = client.post(
        f"/v1/admin/curricula/{imported['version']}/preview",
        json={"mapping": []},
        headers=admin_idempotency_headers(headers),
    ).json()
    url = f"/v1/admin/confirmations/{preview['confirmation_token']}"
    assert client.post(url, json={}, headers=headers).status_code == 422
    key = str(uuid.uuid4())
    first = client.post(url, json={}, headers={**headers, "Idempotency-Key": key})
    replay = client.post(url, json={}, headers={**headers, "Idempotency-Key": key})
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()

    conflict = client.post(
        "/v1/admin/confirmations/different-token",
        json={},
        headers={**headers, "Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_database_failure_does_not_acknowledge_admission(monkeypatch):
    from app import main

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "get_pool", unavailable)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/telegram/updates",
            json=update(next_update_id()),
            headers={"Authorization": "Bearer bot-secret"},
        )
    assert response.status_code == 503
