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


def test_curriculum_module_imports_independently():
    subprocess.run([sys.executable, "-c", "import app.curriculum"], check=True)


def test_shared_help_explains_confirmation_and_topic_selection():
    text = help_text()
    assert (
        "/done [block_id] [actual_minutes] - Record time and close a block after confirmation"
        in text
    )
    assert (
        "/complete_topic [topic_id] - Choose from listed topics when omitted; preview prerequisites and queue impact, then confirm completion"
        in text
    )


def test_all_shared_future_commands_are_recognized_but_not_implemented(client):
    headers = {"Authorization": "Bearer bot-secret"}
    fence = poller(client)
    future_commands = [name for name, _, _ in COMMANDS if name != "help"]

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

    async def persisted():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM operations"
            ), await conn.fetchval("SELECT count(*) FROM deliveries")
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (len(future_commands) + 1, 0)


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
