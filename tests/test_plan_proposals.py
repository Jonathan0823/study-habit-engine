import asyncio
import os
import uuid

import asyncpg
import pytest

from app.db import apply_schema
from app.plan_proposals import (
    PlanProposalError,
    PostgresPlanProposalRepository,
    create_plan_proposal,
)


class FakeSearcher:
    async def search(self, query, *, max_results=5):
        return [
            {
                "title": "The Python Tutorial",
                "publisher": "Python documentation",
                "url": "https://docs.python.org/3/tutorial/",
                "published_at": None,
            }
        ]


class FakeGenerator:
    async def generate(self, goal, context, sources):
        return {
            "goal_summary": goal,
            "duration_weeks": 4,
            "outcomes": [
                {
                    "outcome_id": "write-python",
                    "description": "Write a Python command-line program.",
                    "assessment": "Build and explain a small script.",
                }
            ],
            "topics": [
                {
                    "topic_id": "python-basics",
                    "title": "Python basics",
                    "description": "Practice expressions and functions.",
                    "outcome_ids": ["write-python"],
                    "prerequisites": [],
                    "estimate_minutes": 180,
                    "practice": "Recall syntax, then implement a function without notes.",
                    "resources": [
                        {
                            "title": "Model-invented title",
                            "publisher": "Model-invented publisher",
                            "url": sources[0]["url"],
                            "rationale": "Introduces Python syntax and core language features.",
                        }
                    ],
                }
            ],
        }


class HallucinatingGenerator(FakeGenerator):
    async def generate(self, goal, context, sources):
        plan = await super().generate(goal, context, sources)
        plan["topics"][0]["resources"][0]["url"] = (
            "https://docs.python.org/3/imaginary-page/"
        )
        return plan


class FakeRepository:
    def __init__(self):
        self.saved = []

    async def save_pending(self, operation_id, goal, context, plan):
        proposal_id = "proposal-123"
        self.saved.append((proposal_id, operation_id, goal, context, plan))
        return proposal_id


def test_proposal_workflow_persists_only_a_valid_pending_draft():
    repository = FakeRepository()

    result = asyncio.run(
        create_plan_proposal(
            operation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            goal="Build a small Python command-line application.",
            context={"weekly_minutes": 300},
            searcher=FakeSearcher(),
            generator=FakeGenerator(),
            repository=repository,
            trusted_hosts={"docs.python.org"},
        )
    )

    assert result["proposal_id"] == "proposal-123"
    assert result["status"] == "pending_review"
    assert len(repository.saved) == 1
    assert repository.saved[0][1] == uuid.UUID(
        "00000000-0000-0000-0000-000000000001"
    )
    resource = repository.saved[0][4]["topics"][0]["resources"][0]
    assert resource["url"] == "https://docs.python.org/3/tutorial/"
    assert resource["title"] == "The Python Tutorial"
    assert resource["publisher"] == "Python documentation"
    assert resource["published_at"] is None
    assert resource["checked_at"]


def test_model_cannot_invent_a_page_on_an_allowed_host():
    repository = FakeRepository()

    with pytest.raises(PlanProposalError) as error:
        asyncio.run(
            create_plan_proposal(
                operation_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
                goal="Build a small Python command-line application.",
                context={"weekly_minutes": 300},
                searcher=FakeSearcher(),
                generator=HallucinatingGenerator(),
                repository=repository,
                trusted_hosts={"docs.python.org"},
            )
        )

    assert error.value.code == "INVALID_PLAN"
    assert "RESOURCE_NOT_FROM_SEARCH_RESULTS" in error.value.errors
    assert repository.saved == []


def test_postgres_proposal_save_is_pending_and_idempotent():
    database_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL integration test requires MIGRATION_DATABASE_URL")

    operation_id = uuid.uuid4()

    async def verify():
        await apply_schema(database_url)
        pool = await asyncpg.create_pool(database_url)
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "TRUNCATE learning_plan_proposals, operations CASCADE"
                )
                await conn.execute(
                    "UPDATE engine_state SET active_curriculum_id=NULL WHERE singleton=true"
                )
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'accepted','{}'::jsonb)",
                    operation_id,
                    f"test:{operation_id}",
                    "request-hash",
                )
            repository = PostgresPlanProposalRepository(pool)
            goal = "Build a small Python command-line application."
            context = {"weekly_minutes": 300}
            plan = await FakeGenerator().generate(
                goal,
                context,
                [
                    {
                        "title": "The Python Tutorial",
                        "publisher": "Python documentation",
                        "url": "https://docs.python.org/3/tutorial/",
                        "published_at": None,
                    }
                ],
            )
            first = await repository.save_pending(
                operation_id, goal, context, plan
            )
            replay = await repository.save_pending(
                operation_id, goal, context, plan
            )
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT proposal_id,status FROM learning_plan_proposals"
                )
                active = await conn.fetchval(
                    "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
                )
            return first, replay, rows, active
        finally:
            await pool.close()

    first, replay, rows, active = asyncio.run(verify())
    assert first == replay
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == first
    assert rows[0]["status"] == "pending_review"
    assert active is None
