import asyncio
import json
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.db import apply_schema
from app.main import app
from app.plan_intake import parse_weekly_minutes
from app.plan_proposals import PostgresPlanProposalRepository


@pytest.mark.parametrize(
    ("text", "expected"),
    [("300", 300), ("1", 1), ("10080", 10080), ("0", None), ("10081", None), ("3.5", None), ("٣٠٠", None), ("five", None)],
)
def test_weekly_minutes_requires_ascii_whole_number_in_bounds(text, expected):
    assert parse_weekly_minutes(text) == expected


@pytest.fixture
def client():
    database_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL integration test requires MIGRATION_DATABASE_URL")
    os.environ["BOT_SERVICE_TOKEN"] = "bot-secret"
    os.environ["TELEGRAM_BOT_ID"] = "12345"
    os.environ["TELEGRAM_USER_ID"] = "111"
    os.environ["TELEGRAM_PRIVATE_CHAT_ID"] = "111"

    async def reset_database():
        await apply_schema(database_url)
        conn = await asyncpg.connect(database_url)
        try:
            await conn.execute(
                "TRUNCATE learning_plan_sessions, learning_plan_jobs, learning_plan_proposals, deliveries, telegram_ingestion, operations, telegram_cursor CASCADE"
            )
            await conn.execute(
                "UPDATE engine_state SET active_curriculum_id=NULL WHERE singleton=true"
            )
        finally:
            await conn.close()

    asyncio.run(reset_database())
    with TestClient(app) as test_client:
        yield test_client


_update_id = 0


def new_update_id():
    global _update_id
    _update_id += 1
    return str(_update_id)


def poller(client):
    response = client.post(
        "/v1/telegram/claim",
        json={"bot_id": "12345", "owner_id": str(uuid.uuid4())},
        headers={"Authorization": "Bearer bot-secret"},
    )
    assert response.status_code == 200
    return response.json()["fence"]


def telegram_update(update_id, fence, payload):
    return {
        "bot_id": "12345",
        "update_id": update_id,
        "actor_id": "111",
        "chat_id": "111",
        "chat_type": "private",
        "poller_fence": fence,
        "payload": payload,
    }


def delivered_text(client, operation_id):
    async def fetch():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            payload = await conn.fetchval(
                "SELECT payload FROM deliveries WHERE operation_id=$1",
                uuid.UUID(operation_id),
            )
            return json.loads(payload)["text"] if payload else None
        finally:
            await conn.close()

    return asyncio.run(fetch())


def test_goal_to_plan_intake_persists_one_job_after_goal_and_time_budget(client):
    headers = {"Authorization": "Bearer bot-secret"}
    fence = poller(client)

    start = telegram_update(
        new_update_id(),
        fence,
        {"kind": "command", "command": "learn", "arguments": []},
    )
    started = client.post("/v1/telegram/updates", json=start, headers=headers)
    assert started.status_code == 200
    assert started.json()["status"] == "accepted"
    assert "What do you want to learn?" in delivered_text(
        client, started.json()["operation_id"]
    )

    goal_update = telegram_update(
        new_update_id(),
        fence,
        {"kind": "text", "text": "Learn Python for data automation; I am a beginner."},
    )
    goal_response = client.post(
        "/v1/telegram/updates", json=goal_update, headers=headers
    )
    assert goal_response.status_code == 200
    assert goal_response.json()["status"] == "completed"
    assert "minutes can you study per week" in delivered_text(
        client, goal_response.json()["operation_id"]
    ).lower()

    budget_update = telegram_update(
        new_update_id(),
        fence,
        {"kind": "text", "text": "300"},
    )
    budget_response = client.post(
        "/v1/telegram/updates", json=budget_update, headers=headers
    )
    replay = client.post("/v1/telegram/updates", json=budget_update, headers=headers)
    assert budget_response.status_code == 200
    assert budget_response.json()["status"] == "completed"
    assert replay.json() == budget_response.json()

    async def verify():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            jobs = await conn.fetch(
                "SELECT operation_id,goal,learner_context,status FROM learning_plan_jobs"
            )
            sessions = await conn.fetchval("SELECT count(*) FROM learning_plan_sessions")
            proposals = await conn.fetchval(
                "SELECT count(*) FROM learning_plan_proposals"
            )
            active = await conn.fetchval(
                "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
            )
            return jobs, sessions, proposals, active
        finally:
            await conn.close()

    jobs, sessions, proposals, active = asyncio.run(verify())
    assert len(jobs) == 1
    assert jobs[0]["operation_id"] == uuid.UUID(budget_response.json()["operation_id"])
    assert jobs[0]["goal"] == "Learn Python for data automation; I am a beginner."
    assert json.loads(jobs[0]["learner_context"])["weekly_minutes"] == 300
    assert jobs[0]["status"] == "pending"
    assert sessions == 0
    assert proposals == 0
    assert active is None


def test_telegram_payload_accepts_4096_char_messages_and_unbounded_goal_tokens(client):
    headers = {"Authorization": "Bearer bot-secret"}
    fence = poller(client)

    long_token = client.post(
        "/v1/telegram/updates",
        json=telegram_update(
            new_update_id(),
            fence,
            {"kind": "command", "command": "learn", "arguments": ["x" * 2050]},
        ),
        headers=headers,
    )
    assert long_token.status_code == 200
    assert long_token.json()["result"]["code"] == "INVALID_LEARNING_GOAL"
    assert "up to 2,000 characters" in delivered_text(
        client, long_token.json()["operation_id"]
    )

    oversized_text = client.post(
        "/v1/telegram/updates",
        json=telegram_update(
            new_update_id(), fence, {"kind": "text", "text": "x" * 4096}
        ),
        headers=headers,
    )
    assert oversized_text.status_code == 200
    assert oversized_text.json()["result"]["code"] == "INVALID_LEARNING_GOAL"
    assert "up to 2,000 characters" in delivered_text(
        client, oversized_text.json()["operation_id"]
    )

    many_tokens = ["goal"] * 40
    accepted = client.post(
        "/v1/telegram/updates",
        json=telegram_update(
            new_update_id(),
            fence,
            {"kind": "command", "command": "learn", "arguments": many_tokens},
        ),
        headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    async def fetch_session():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchrow(
                "SELECT phase,goal FROM learning_plan_sessions WHERE actor_id=111 AND chat_id=111"
            )
        finally:
            await conn.close()

    session = asyncio.run(fetch_session())
    assert session["phase"] == "awaiting_weekly_minutes"
    assert session["goal"] == " ".join(many_tokens)


def test_telegram_approval_activates_and_rejection_preserves_active_curriculum(
    client, monkeypatch
):
    database_url = os.environ["DATABASE_URL"]

    async def create_proposal(label):
        operation_id = uuid.uuid4()
        pool = await asyncpg.create_pool(database_url)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'completed','{}'::jsonb)",
                    operation_id,
                    f"test-proposal:{label}:{operation_id}",
                    f"proposal:{operation_id}",
                )
            plan = {
                "goal_summary": "Write a Python command-line program.",
                "duration_weeks": 4,
                "outcomes": [
                    {
                        "outcome_id": "write-program",
                        "description": "Write a small Python program.",
                        "assessment": "Build and explain the program.",
                    }
                ],
                "topics": [
                    {
                        "topic_id": f"topic-{label}",
                        "title": "Python basics",
                        "description": "Practice expressions and functions.",
                        "outcome_ids": ["write-program"],
                        "prerequisites": [],
                        "estimate_minutes": 180,
                        "practice": "Implement a function without notes.",
                        "resources": [
                            {
                                "title": "The Python Tutorial",
                                "publisher": "Python documentation",
                                "url": "https://docs.python.org/3/tutorial/",
                                "rationale": "Introduces core Python concepts.",
                            }
                        ],
                    }
                ],
                "quality_review": [
                    {
                        "criterion": "goal_coverage",
                        "evidence": "The outcome is covered by the Python topic.",
                        "topic_ids": [f"topic-{label}"],
                        "outcome_ids": ["write-program"],
                    },
                    {
                        "criterion": "objective_assessment_alignment",
                        "evidence": "The build task assesses the outcome.",
                        "topic_ids": [f"topic-{label}"],
                        "outcome_ids": ["write-program"],
                    },
                    {
                        "criterion": "topic_sequence",
                        "evidence": "The topic has no unmet prerequisites.",
                        "topic_ids": [f"topic-{label}"],
                        "outcome_ids": [],
                    },
                    {
                        "criterion": "practice_and_retrieval",
                        "evidence": "The implementation task requires active practice.",
                        "topic_ids": [f"topic-{label}"],
                        "outcome_ids": [],
                    },
                    {
                        "criterion": "scope_and_workload",
                        "evidence": "The estimate fits the weekly budget.",
                        "topic_ids": [f"topic-{label}"],
                        "outcome_ids": [],
                    },
                ],
            }
            repository = PostgresPlanProposalRepository(pool)
            return await repository.save_pending(
                operation_id,
                "Learn Python programming.",
                {"weekly_minutes": 300},
                plan,
            )
        finally:
            await pool.close()

    approved_id = asyncio.run(create_proposal("approve"))
    rejected_id = asyncio.run(create_proposal("reject"))
    headers = {"Authorization": "Bearer bot-secret"}
    fence = poller(client)
    missing_id = client.post(
        "/v1/telegram/updates",
        json=telegram_update(
            new_update_id(), fence, {"kind": "command", "command": "approve", "arguments": []}
        ),
        headers=headers,
    )
    assert missing_id.json()["result"]["code"] == "PROPOSAL_ID_REQUIRED"

    approved = client.post(
        "/v1/telegram/updates",
        json=telegram_update(
            new_update_id(),
            fence,
            {"kind": "command", "command": "approve", "arguments": [str(approved_id)]},
        ),
        headers=headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    assert approved.json()["result"]["code"] == "LEARNING_PLAN_APPROVED"

    async def active_curriculum_id():
        conn = await asyncpg.connect(database_url)
        try:
            return await conn.fetchval(
                "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
            )
        finally:
            await conn.close()

    active_id = asyncio.run(active_curriculum_id())
    assert active_id is not None

    rejected = client.post(
        "/v1/telegram/updates",
        json=telegram_update(
            new_update_id(),
            fence,
            {"kind": "command", "command": "reject", "arguments": [str(rejected_id)]},
        ),
        headers=headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "completed"
    assert rejected.json()["result"]["code"] == "LEARNING_PLAN_REJECTED"
    assert asyncio.run(active_curriculum_id()) == active_id

    invalid_id = asyncio.run(create_proposal("validation-error"))
    from app.curriculum import Curriculum

    def fail_validation(cls, value, **kwargs):
        raise ValueError("simulated curriculum validation failure")

    monkeypatch.setattr(Curriculum, "model_validate", classmethod(fail_validation))
    failed = client.post(
        "/v1/telegram/updates",
        json=telegram_update(
            new_update_id(),
            fence,
            {"kind": "command", "command": "approve", "arguments": [str(invalid_id)]},
        ),
        headers=headers,
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "completed"
    assert failed.json()["result"]["code"] == "PLAN_PROPOSAL_FAILED_VALIDATION"
    assert asyncio.run(active_curriculum_id()) == active_id
