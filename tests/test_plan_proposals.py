import asyncio
import os
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest

from app.db import apply_schema
from app.plan_approval import decide_plan_proposal, parse_proposal_id
from app.plan_jobs import (
    format_plan_for_review,
    process_one_learning_plan_job,
    reserve_tavily_credit,
)
from app.plan_proposals import (
    PlanProposalError,
    PostgresPlanProposalRepository,
    create_plan_proposal,
)


def test_proposal_id_parser_requires_exactly_one_uuid():
    proposal_id = uuid.uuid4()
    assert parse_proposal_id([str(proposal_id)]) == proposal_id
    assert parse_proposal_id([]) is None
    assert parse_proposal_id(["not-a-uuid"]) is None
    assert parse_proposal_id([str(proposal_id), "extra"]) is None


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


class NoTrustedSources:
    async def search(self, query, *, max_results=5):
        return []


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
            "quality_review": [
                {
                    "criterion": "goal_coverage",
                    "evidence": "The stated outcome is covered by the Python topic.",
                    "topic_ids": ["python-basics"],
                    "outcome_ids": ["write-python"],
                },
                {
                    "criterion": "objective_assessment_alignment",
                    "evidence": "The implementation task assesses the outcome.",
                    "topic_ids": ["python-basics"],
                    "outcome_ids": ["write-python"],
                },
                {
                    "criterion": "topic_sequence",
                    "evidence": "The foundational topic has no unmet prerequisites.",
                    "topic_ids": ["python-basics"],
                    "outcome_ids": [],
                },
                {
                    "criterion": "practice_and_retrieval",
                    "evidence": "The topic includes recall before independent coding.",
                    "topic_ids": ["python-basics"],
                    "outcome_ids": [],
                },
                {
                    "criterion": "scope_and_workload",
                    "evidence": "The estimate fits the supplied weekly budget.",
                    "topic_ids": ["python-basics"],
                    "outcome_ids": [],
                },
            ],
        }


def test_approval_warnings_stay_with_approve_command_when_proposal_is_chunked():
    proposal_id = uuid.uuid4()
    sources = asyncio.run(FakeSearcher().search("Python"))
    plan = asyncio.run(FakeGenerator().generate("Learn Python", {}, sources))
    plan["topics"][0]["description"] = "x" * 6191

    chunks = format_plan_for_review(proposal_id, plan)
    assert len(chunks) > 1
    warning_chunk = next(
        chunk
        for chunk in chunks
        if "Approval replaces any currently active curriculum." in chunk
    )

    assert (
        "Historical minutes remain with prior topic IDs; no completion is carried over."
        in warning_chunk
    )
    assert f"/approve {proposal_id}" in warning_chunk


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
    assert repository.saved[0][1] == uuid.UUID("00000000-0000-0000-0000-000000000001")
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
            first = await repository.save_pending(operation_id, goal, context, plan)
            replay = await repository.save_pending(operation_id, goal, context, plan)
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


def test_learning_plan_job_persists_proposal_and_enforces_search_cap():
    database_url = os.environ.get("MIGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL integration test requires MIGRATION_DATABASE_URL")

    operation_id = uuid.uuid4()
    month_start = datetime.now(timezone.utc).date().replace(day=1)

    async def verify():
        await apply_schema(database_url)
        pool = await asyncpg.create_pool(database_url)
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "TRUNCATE learning_plan_jobs,learning_plan_sessions,learning_plan_proposals,tavily_search_usage,deliveries,operations CASCADE"
                )
                await conn.execute(
                    "UPDATE engine_state SET active_curriculum_id=NULL WHERE singleton=true"
                )
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'accepted',$4::jsonb)",
                    operation_id,
                    f"test:{operation_id}",
                    "request-hash",
                    '{"status":"accepted","result":{"code":"PLAN_ACCEPTED"}}',
                )
                await conn.execute(
                    "INSERT INTO learning_plan_jobs(job_id,operation_id,goal,learner_context) VALUES($1,$2,$3,$4::jsonb)",
                    uuid.uuid4(),
                    operation_id,
                    "Build a small Python command-line application.",
                    '{"weekly_minutes":300}',
                )
            processed = await process_one_learning_plan_job(
                pool,
                uuid.uuid4(),
                searcher=FakeSearcher(),
                generator=FakeGenerator(),
                monthly_search_limit=1,
            )
            allowed_replay = await reserve_tavily_credit(pool, month_start, limit=1)
            async with pool.acquire() as conn:
                job = await conn.fetchrow(
                    "SELECT status,proposal_id FROM learning_plan_jobs WHERE operation_id=$1",
                    operation_id,
                )
                proposal = await conn.fetchrow(
                    "SELECT proposal_id,status FROM learning_plan_proposals WHERE operation_id=$1",
                    operation_id,
                )
                operation = await conn.fetchrow(
                    "SELECT status,result FROM operations WHERE operation_id=$1",
                    operation_id,
                )
                messages = await conn.fetch(
                    "SELECT payload FROM deliveries WHERE operation_id=$1 ORDER BY logical_key",
                    operation_id,
                )
                credits = await conn.fetchval(
                    "SELECT credits_reserved FROM tavily_search_usage WHERE month_start=$1",
                    month_start,
                )
                active_before = await conn.fetchval(
                    "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
                )
            approval_operation_id = uuid.uuid4()
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'accepted','{}'::jsonb)",
                    approval_operation_id,
                    f"test-approval:{approval_operation_id}",
                    "approval-request-hash",
                )
                await decide_plan_proposal(
                    conn, approval_operation_id, proposal["proposal_id"], approve=True
                )
            rejected_source_operation = uuid.uuid4()
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'accepted','{}'::jsonb)",
                    rejected_source_operation,
                    f"test-rejected-source:{rejected_source_operation}",
                    "rejected-source-hash",
                )
            rejected_proposal = await create_plan_proposal(
                operation_id=rejected_source_operation,
                goal="Learn introductory programming.",
                context={"weekly_minutes": 300},
                searcher=FakeSearcher(),
                generator=FakeGenerator(),
                repository=PostgresPlanProposalRepository(pool),
                trusted_hosts={"docs.python.org"},
            )
            rejection_operation = uuid.uuid4()
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'accepted','{}'::jsonb)",
                    rejection_operation,
                    f"test-rejection:{rejection_operation}",
                    "rejection-hash",
                )
                await decide_plan_proposal(
                    conn,
                    rejection_operation,
                    uuid.UUID(rejected_proposal["proposal_id"]),
                    approve=False,
                )
            async with pool.acquire() as conn:
                final_proposal = await conn.fetchrow(
                    "SELECT status,curriculum_id FROM learning_plan_proposals WHERE proposal_id=$1",
                    proposal["proposal_id"],
                )
                version = await conn.fetchrow(
                    "SELECT status,content FROM curriculum_versions WHERE curriculum_id=$1",
                    final_proposal["curriculum_id"],
                )
                active_after = await conn.fetchval(
                    "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
                )
                rejected_status = await conn.fetchval(
                    "SELECT status FROM learning_plan_proposals WHERE proposal_id=$1",
                    uuid.UUID(rejected_proposal["proposal_id"]),
                )
            return (
                processed,
                allowed_replay,
                job,
                proposal,
                operation,
                messages,
                credits,
                active_before,
                final_proposal,
                version,
                active_after,
                rejected_status,
            )
        finally:
            await pool.close()

    (
        processed,
        allowed_replay,
        job,
        proposal,
        operation,
        messages,
        credits,
        active_before,
        final_proposal,
        version,
        active_after,
        rejected_status,
    ) = asyncio.run(verify())
    assert processed is True
    assert allowed_replay is False
    assert job["status"] == "completed"
    assert job["proposal_id"] == proposal["proposal_id"]
    assert proposal["status"] == "pending_review"
    assert operation["status"] == "completed"
    assert len(messages) >= 1
    rendered = " ".join(row["payload"] for row in messages)
    assert "/approve " in rendered
    assert "Python documentation" in rendered
    assert "Why it may help (AI rationale)" in rendered
    assert "AI self-review notes" in rendered
    assert "Approval replaces any currently active curriculum." in rendered
    assert (
        "Historical minutes remain with prior topic IDs; no completion is carried over."
        in rendered
    )
    assert rendered.index(
        "Approval replaces any currently active curriculum."
    ) < rendered.index("/approve ")
    assert "cmu.edu/teaching" in rendered
    assert credits == 1
    assert active_before is None
    assert final_proposal["status"] == "approved"
    assert final_proposal["curriculum_id"] == active_after
    assert version["status"] == "approved"
    assert rejected_status == "rejected"


def test_no_trusted_sources_reports_current_scope_and_preserves_active_plan():
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
                    "TRUNCATE learning_plan_jobs,learning_plan_sessions,learning_plan_proposals,tavily_search_usage,deliveries,operations CASCADE"
                )
                active_before = await conn.fetchval(
                    "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
                )
                await conn.execute(
                    "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,'accepted','{}'::jsonb)",
                    operation_id,
                    f"unsupported-goal:{operation_id}",
                    "request-hash",
                )
                await conn.execute(
                    "INSERT INTO learning_plan_jobs(job_id,operation_id,goal,learner_context) VALUES($1,$2,$3,$4::jsonb)",
                    uuid.uuid4(),
                    operation_id,
                    "Learn piano",
                    '{"weekly_minutes":300}',
                )
            await process_one_learning_plan_job(
                pool,
                uuid.uuid4(),
                searcher=NoTrustedSources(),
                generator=FakeGenerator(),
            )
            async with pool.acquire() as conn:
                job = await conn.fetchrow(
                    "SELECT status,error_code FROM learning_plan_jobs WHERE operation_id=$1",
                    operation_id,
                )
                proposal_count = await conn.fetchval(
                    "SELECT count(*) FROM learning_plan_proposals WHERE operation_id=$1",
                    operation_id,
                )
                message = await conn.fetchval(
                    "SELECT payload->>'text' FROM deliveries WHERE operation_id=$1",
                    operation_id,
                )
                active_after = await conn.fetchval(
                    "SELECT active_curriculum_id FROM engine_state WHERE singleton=true"
                )
            return job, proposal_count, message, active_before, active_after
        finally:
            await pool.close()

    job, proposal_count, message, active_before, active_after = asyncio.run(verify())
    assert job["status"] == "failed"
    assert job["error_code"] == "NO_TRUSTED_SOURCES"
    assert proposal_count == 0
    assert "programming and computer science" in message.lower()
    assert "no curriculum was changed" in message.lower()
    assert active_after == active_before
