"""Orchestrate one bounded learning-plan proposal attempt."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone

from app.learning_plans import validate_learning_plan
from app.source_search import trusted_https_host


class PostgresPlanProposalRepository:
    def __init__(self, pool):
        self._pool = pool

    async def save_pending(
        self, operation_id: uuid.UUID, goal: str, context: dict, plan: dict
    ) -> uuid.UUID:
        request = json.dumps(
            {"goal": goal, "context": context, "plan": plan},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        request_hash = hashlib.sha256(request.encode("utf-8")).hexdigest()
        async with self._pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT proposal_id,content_hash FROM learning_plan_proposals WHERE operation_id=$1 FOR UPDATE",
                operation_id,
            )
            if existing is not None:
                if existing["content_hash"] != request_hash:
                    raise PlanProposalError("IDEMPOTENCY_CONFLICT")
                return existing["proposal_id"]
            proposal_id = uuid.uuid4()
            inserted = await conn.fetchval(
                """INSERT INTO learning_plan_proposals
                   (proposal_id,operation_id,goal,learner_context,content,content_hash)
                   VALUES($1,$2,$3,$4::jsonb,$5::jsonb,$6)
                   ON CONFLICT(operation_id) DO NOTHING
                   RETURNING proposal_id""",
                proposal_id,
                operation_id,
                goal,
                json.dumps(context),
                json.dumps(plan),
                request_hash,
            )
            if inserted is not None:
                return inserted
            existing = await conn.fetchrow(
                "SELECT proposal_id,content_hash FROM learning_plan_proposals WHERE operation_id=$1 FOR UPDATE",
                operation_id,
            )
            if existing is None or existing["content_hash"] != request_hash:
                raise PlanProposalError("IDEMPOTENCY_CONFLICT")
            return existing["proposal_id"]


class PlanProposalError(ValueError):
    def __init__(self, code: str, errors: list[str] | None = None):
        super().__init__(code)
        self.code = code
        self.errors = errors or []


async def create_plan_proposal(
    *,
    operation_id: uuid.UUID,
    goal: str,
    context: dict,
    searcher,
    generator,
    repository,
    trusted_hosts: set[str],
) -> dict:
    """Search trusted sources, draft and validate a plan, then persist for review.

    Search and model adapters are each called at most once. A proposal is saved
    only after every structural, time-budget, and candidate-source check passes.
    This workflow never activates or edits the learner's active curriculum.
    """
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        raise PlanProposalError("INVALID_GOAL")
    if not isinstance(context, dict):
        raise PlanProposalError("INVALID_CONTEXT")
    weekly_minutes = context.get("weekly_minutes")
    if type(weekly_minutes) is not int or not 1 <= weekly_minutes <= 10080:
        raise PlanProposalError("INVALID_WEEKLY_BUDGET")

    source_results = await asyncio.wait_for(
        searcher.search(goal.strip(), max_results=10), timeout=10
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    sources = []
    for source in source_results:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        if trusted_https_host(url, trusted_hosts) is None:
            continue
        title = source.get("title")
        publisher = source.get("publisher")
        if not isinstance(title, str) or not title.strip():
            continue
        sources.append(
            {
                "title": title.strip()[:500],
                "publisher": publisher.strip()[:500]
                if isinstance(publisher, str) and publisher.strip()
                else trusted_https_host(url, trusted_hosts),
                "url": url,
                "published_at": source.get("published_at"),
                "checked_at": checked_at,
            }
        )
    if not sources:
        raise PlanProposalError("NO_TRUSTED_SOURCES")

    generated = await asyncio.wait_for(
        generator.generate(goal.strip(), context, sources), timeout=20
    )
    if hasattr(generated, "model_dump"):
        generated = generated.model_dump(exclude_none=True)
    if isinstance(generated, str):
        try:
            generated = json.loads(generated)
        except (json.JSONDecodeError, TypeError):
            raise PlanProposalError("INVALID_MODEL_OUTPUT") from None
    if not isinstance(generated, dict):
        raise PlanProposalError("INVALID_MODEL_OUTPUT")
    candidate_by_url = {source["url"]: source for source in sources}
    for topic in generated.get("topics", []):
        if not isinstance(topic, dict) or not isinstance(topic.get("resources"), list):
            continue
        for resource in topic["resources"]:
            if not isinstance(resource, dict):
                continue
            candidate = candidate_by_url.get(resource.get("url"))
            if candidate is not None:
                resource.update(
                    title=candidate["title"],
                    publisher=candidate["publisher"],
                    published_at=candidate["published_at"],
                    checked_at=candidate["checked_at"],
                )
    candidate_urls = set(candidate_by_url)
    errors = validate_learning_plan(
        generated,
        trusted_hosts=trusted_hosts,
        weekly_minutes=weekly_minutes,
        candidate_urls=candidate_urls,
    )
    if errors:
        raise PlanProposalError("INVALID_PLAN", errors)

    proposal_id = await repository.save_pending(
        operation_id, goal.strip(), context, generated
    )
    return {
        "proposal_id": str(proposal_id),
        "status": "pending_review",
        "plan": generated,
    }
