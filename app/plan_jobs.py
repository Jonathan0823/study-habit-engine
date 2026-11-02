"""Leased worker for durable learning-plan proposal requests."""

from __future__ import annotations

import json
import textwrap
import uuid
from datetime import datetime, timezone

from app.learning_plans import QUALITY_REFERENCES
from app.plan_proposals import (
    PlanProposalError,
    PostgresPlanProposalRepository,
    create_plan_proposal,
)
from app.trusted_sources import SOFTWARE_CS_TRUSTED_HOSTS


async def reserve_tavily_credit(pool, month_start, *, limit: int = 1000) -> bool:
    if type(limit) is not int or not 1 <= limit <= 1000:
        return False
    async with pool.acquire() as conn:
        reserved = await conn.fetchval(
            """INSERT INTO tavily_search_usage(month_start,credits_reserved)
               VALUES($1,1)
               ON CONFLICT(month_start) DO UPDATE SET
                 credits_reserved=tavily_search_usage.credits_reserved+1,
                 updated_at=now()
               WHERE tavily_search_usage.credits_reserved < $2
               RETURNING credits_reserved""",
            month_start,
            limit,
        )
    return reserved is not None


def format_plan_for_review(proposal_id: uuid.UUID | str, plan: dict) -> list[str]:
    proposal_id = str(proposal_id)
    lines = [
        f"Learning-plan proposal {proposal_id}",
        f"Goal: {plan['goal_summary']}",
        f"Estimated duration: {plan['duration_weeks']} weeks",
        "",
        "Learning outcomes:",
    ]
    for outcome in plan["outcomes"]:
        lines.append(f"- {outcome['description']} (check: {outcome['assessment']})")
    lines.extend(["", "AI self-review notes (not independently verified):"])
    for item in plan["quality_review"]:
        criterion = item["criterion"].replace("_", " ").title()
        lines.append(f"- {criterion}: {item['evidence']}")
        lines.append(
            "  Related topics: "
            + ", ".join(item["topic_ids"])
            + "; outcomes: "
            + (", ".join(item["outcome_ids"]) or "not applicable")
        )
    lines.extend(["", "Study topics:"])
    for index, topic in enumerate(plan["topics"], start=1):
        prerequisites = ", ".join(topic["prerequisites"]) or "none"
        lines.extend(
            [
                f"{index}. {topic['title']} — about {topic['estimate_minutes']} minutes",
                f"   {topic['description']}",
                f"   Practice: {topic['practice']}",
                f"   Prerequisites: {prerequisites}",
            ]
        )
        for resource in topic["resources"]:
            lines.extend(
                [
                    f"   Suggested source: {resource['title']} — {resource['publisher']}",
                    f"   Link: {resource['url']}",
                    f"   Why it may help (AI rationale): {resource['rationale']}",
                    "   Suggestion only; this is not a verified endorsement.",
                ]
            )
    lines.extend(
        [
            "",
            "Review references:",
            *[
                f"- {reference['title']}: {reference['url']}"
                for reference in QUALITY_REFERENCES
            ],
            "",
            "This draft is not active until you approve it.",
            "Approval replaces any currently active curriculum.",
            "Historical minutes remain with prior topic IDs; no completion is carried over.",
            f"Approve with /approve {proposal_id}",
            f"Reject with /reject {proposal_id}",
        ]
    )

    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, width=3400) or [""])
    chunks = []
    current = ""
    for line in wrapped:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > 3800 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def process_one_learning_plan_job(
    pool,
    owner: uuid.UUID,
    *,
    searcher,
    generator,
    monthly_search_limit: int = 1000,
) -> bool:
    """Claim and process at most one plan job; return whether one was claimed."""
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            """SELECT * FROM learning_plan_jobs
               WHERE status='pending' OR (status='running' AND lease_until<=now())
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
        )
        if job is None:
            return False
        job = await conn.fetchrow(
            """UPDATE learning_plan_jobs
               SET status='running',lease_owner=$2,fence=fence+1,
                   lease_until=now()+interval '2 minutes',attempts=attempts+1
               WHERE job_id=$1 RETURNING *""",
            job["job_id"],
            owner,
        )

    code = None
    proposal = None
    if searcher is None or generator is None:
        code = "PLAN_GENERATION_NOT_CONFIGURED"
    else:
        month_start = datetime.now(timezone.utc).date().replace(day=1)
        if not await reserve_tavily_credit(
            pool, month_start, limit=monthly_search_limit
        ):
            code = "TAVILY_MONTHLY_BUDGET_EXHAUSTED"
        else:
            context = job["learner_context"]
            if isinstance(context, str):
                context = json.loads(context)
            try:
                proposal = await create_plan_proposal(
                    operation_id=job["operation_id"],
                    goal=job["goal"],
                    context=context,
                    searcher=searcher,
                    generator=generator,
                    repository=PostgresPlanProposalRepository(pool),
                    trusted_hosts=set(SOFTWARE_CS_TRUSTED_HOSTS),
                )
            except PlanProposalError as exc:
                code = exc.code
            except TimeoutError:
                code = "PLAN_GENERATION_TIMEOUT"
            except Exception:
                code = "PLAN_GENERATION_UNAVAILABLE"

    async with pool.acquire() as conn, conn.transaction():
        current = await conn.fetchrow(
            "SELECT status,lease_owner,fence FROM learning_plan_jobs WHERE job_id=$1 FOR UPDATE",
            job["job_id"],
        )
        if (
            current is None
            or current["status"] != "running"
            or current["lease_owner"] != owner
            or current["fence"] != job["fence"]
        ):
            return True
        if proposal is None:
            error_code = code or "PLAN_GENERATION_UNAVAILABLE"
            await conn.execute(
                """UPDATE learning_plan_jobs SET status='failed',error_code=$2,
                   lease_until=NULL WHERE job_id=$1""",
                job["job_id"],
                error_code,
            )
            await conn.execute(
                """UPDATE operations SET status='completed',
                   result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),
                   '{result}',jsonb_build_object('code',$2::text))
                   WHERE operation_id=$1""",
                job["operation_id"],
                error_code,
            )
            message = (
                "I couldn't find trusted sources in the current programming and "
                "computer science catalog for that goal. /learn currently supports "
                "programming and computer science only; no curriculum was changed. "
                "Try a supported goal or add detail."
                if error_code == "NO_TRUSTED_SOURCES"
                else "I couldn't prepare a safe learning-plan proposal right now. "
                "No curriculum was changed. Please try /learn again later."
            )
            await _insert_delivery(conn, job["operation_id"], "failure", message)
            return True

        proposal_id = uuid.UUID(proposal["proposal_id"])
        await conn.execute(
            """UPDATE learning_plan_jobs SET status='completed',proposal_id=$2,
               lease_until=NULL,completed_at=now() WHERE job_id=$1""",
            job["job_id"],
            proposal_id,
        )
        await conn.execute(
            """UPDATE operations SET status='completed',
               result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),
               '{result}',jsonb_build_object('code','LEARNING_PLAN_PROPOSAL_READY',
               'proposal_id',$2::text)) WHERE operation_id=$1""",
            job["operation_id"],
            str(proposal_id),
        )
        for index, message in enumerate(
            format_plan_for_review(proposal_id, proposal["plan"])
        ):
            await _insert_delivery(
                conn, job["operation_id"], f"proposal:{index}", message
            )
    return True


async def _insert_delivery(conn, operation_id, suffix: str, text: str):
    await conn.execute(
        """INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload)
           VALUES($1,$2,$3,'learning_plan',jsonb_build_object('text',$4::text))
           ON CONFLICT(logical_key) DO NOTHING""",
        uuid.uuid4(),
        f"operation:{operation_id}:learning-plan:{suffix}",
        operation_id,
        text,
    )
