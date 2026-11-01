import asyncio
import json
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.commands import COMMANDS, help_text
from app.db import apply_schema
from app.main import app


@pytest.fixture
def client():
    database_url = os.environ["MIGRATION_DATABASE_URL"]
    async def reset_database():
        await apply_schema(database_url)
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute("TRUNCATE deliveries, telegram_ingestion, operations, telegram_cursor CASCADE")
        finally:
            await conn.close()

    asyncio.run(reset_database())
    with TestClient(app) as test_client:
        yield test_client


def update(update_id: str, *, command: str = "help", actor_id: str = "111", chat_id: str = "111") -> dict:
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
    response = client.post("/v1/telegram/claim", json={"bot_id": "12345", "owner_id": str(uuid.uuid4())},
                           headers={"Authorization": "Bearer bot-secret"})
    assert response.status_code == 200
    return response.json()["fence"]


def test_help_is_admitted_idempotently_and_outboxed(client):
    headers = {"Authorization": "Bearer bot-secret"}
    body = update("67890")
    body["poller_fence"] = poller(client)
    first = client.post("/v1/telegram/updates", json=body, headers=headers)
    duplicate = client.post("/v1/telegram/updates", json=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert duplicate.json() == first.json()
    async def persisted():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            deliveries = await conn.fetch("SELECT operation_id, payload, status FROM deliveries")
            operations = await conn.fetchval("SELECT count(*) FROM operations")
            next_offset = await conn.fetchval("SELECT next_offset FROM telegram_cursor WHERE bot_id=12345")
            return deliveries, operations, next_offset
        finally:
            await conn.close()

    deliveries, operations, next_offset = asyncio.run(persisted())
    assert operations == 1
    assert next_offset == 67891
    assert len(deliveries) == 1
    assert deliveries[0]["operation_id"] == uuid.UUID(first.json()["operation_id"])
    assert json.loads(deliveries[0]["payload"])["text"] == help_text()
    assert deliveries[0]["status"] == "pending"


def test_shared_help_explains_confirmation_and_topic_selection():
    text = help_text()
    assert "/done [block_id] [actual_minutes] - Record time and close a block after confirmation" in text
    assert "/complete_topic [topic_id] - Choose from listed topics when omitted; preview prerequisites and queue impact, then confirm completion" in text


def test_all_shared_future_commands_are_recognized_but_not_implemented(client):
    headers = {"Authorization": "Bearer bot-secret"}
    fence = poller(client)
    future_commands = [name for name, _, _ in COMMANDS if name != "help"]

    for update_id, command in enumerate(future_commands, start=68000):
        body = update(str(update_id), command=command)
        body["poller_fence"] = fence
        response = client.post("/v1/telegram/updates", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        assert response.json()["result"]["code"] == "NOT_IMPLEMENTED"
        assert response.json()["callback_notice"] is None

    body = update("68010", command="not_a_command")
    body["poller_fence"] = fence
    unsupported = client.post("/v1/telegram/updates", json=body, headers=headers)
    assert unsupported.status_code == 200
    assert unsupported.json()["result"]["code"] == "UNSUPPORTED_COMMAND"

    async def persisted():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval("SELECT count(*) FROM operations"), await conn.fetchval("SELECT count(*) FROM deliveries")
        finally:
            await conn.close()

    assert asyncio.run(persisted()) == (len(future_commands) + 1, 0)


def test_update_id_with_different_content_conflicts(client):
    fence = poller(client)
    body = update("67889")
    body["poller_fence"] = fence
    headers = {"Authorization": "Bearer bot-secret"}
    assert client.post("/v1/telegram/updates", json=body, headers=headers).status_code == 200
    body["payload"]["arguments"] = ["changed"]
    assert client.post("/v1/telegram/updates", json=body, headers=headers).status_code == 409


def test_unauthorized_update_is_disposed_without_help_or_learning_state(client):
    body = update("67891", actor_id="222", chat_id="222")
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
            return await conn.fetchval("SELECT count(*) FROM operations"), await conn.fetchval("SELECT count(*) FROM deliveries")
        finally:
            await conn.close()

    operations, deliveries = asyncio.run(persisted())
    assert operations == 1
    assert deliveries == 0


def test_group_is_rejected_without_creating_delivery(client):
    body = update("67892")
    body["poller_fence"] = poller(client)
    body["chat_type"] = "group"
    response = client.post("/v1/telegram/updates", json=body, headers={"Authorization": "Bearer bot-secret"})
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_delivery_retry_reuses_the_original_operation(client):
    headers = {"Authorization": "Bearer bot-secret"}
    body = update("67893")
    body["poller_fence"] = poller(client)
    accepted = client.post("/v1/telegram/updates", json=body, headers=headers).json()
    delivery = client.post("/v1/deliveries/claim", headers=headers).json()
    failed = client.post(f"/v1/deliveries/{delivery['delivery_id']}/result", json={
        "fence": delivery["fence"], "outcome": "transient_failure"
    }, headers=headers)
    assert failed.status_code == 200
    assert client.post("/v1/deliveries/claim", headers=headers).status_code == 204

    async def make_retry_due():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute("UPDATE deliveries SET next_attempt_at=now() WHERE delivery_id=$1", uuid.UUID(delivery["delivery_id"]))
        finally:
            await conn.close()

    asyncio.run(make_retry_due())
    retried_delivery = client.post("/v1/deliveries/claim", headers=headers).json()
    assert retried_delivery["delivery_id"] == delivery["delivery_id"]
    assert retried_delivery["operation_id"] == accepted["operation_id"]

    async def persisted_counts():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval("SELECT count(*) FROM operations"), await conn.fetchval("SELECT count(*) FROM deliveries")
        finally:
            await conn.close()

    assert asyncio.run(persisted_counts()) == (1, 1)


def test_database_failure_does_not_acknowledge_admission(monkeypatch):
    from app import main

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "get_pool", unavailable)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/telegram/updates",
            json=update("67900"),
            headers={"Authorization": "Bearer bot-secret"},
        )
    assert response.status_code == 503
