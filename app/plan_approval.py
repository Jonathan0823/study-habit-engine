"""Explicit Telegram approval/rejection of immutable plan proposals."""

from __future__ import annotations

import hashlib
import json
import uuid

from app.curriculum import Curriculum, canonical_content, curriculum_impact
from app.learning_plans import validate_learning_plan
from app.trusted_sources import SOFTWARE_CS_TRUSTED_HOSTS


def parse_proposal_id(arguments: list[str]) -> uuid.UUID | None:
    if len(arguments) != 1:
        return None
    try:
        return uuid.UUID(arguments[0])
    except (ValueError, AttributeError):
        return None


async def decide_plan_proposal(
    conn,
    operation_id: uuid.UUID,
    proposal_id: uuid.UUID | None,
    *,
    approve: bool,
):
    if proposal_id is None:
        command = "/approve <proposal_id>" if approve else "/reject <proposal_id>"
        await _finish(
            conn,
            operation_id,
            "PROPOSAL_ID_REQUIRED",
            f"Use {command} with the proposal ID shown in its review message.",
        )
        return
    if approve:
        days = await conn.fetch("SELECT study_date FROM study_days ORDER BY study_date")
        for day in days:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(0,$1::date - DATE '2000-01-01')",
                day["study_date"],
            )
        await conn.fetch(
            "SELECT job_id FROM planning_jobs WHERE status IN ('pending','running') ORDER BY job_id FOR UPDATE"
        )
        state = await conn.fetchrow(
            "SELECT * FROM engine_state WHERE singleton=true FOR UPDATE"
        )
        await conn.fetch("SELECT study_date FROM study_days ORDER BY study_date FOR UPDATE")

    proposal = await conn.fetchrow(
        "SELECT * FROM learning_plan_proposals WHERE proposal_id=$1 FOR UPDATE",
        proposal_id,
    )
    if proposal is None:
        await _finish(
            conn,
            operation_id,
            "PLAN_PROPOSAL_NOT_FOUND",
            "I couldn't find that learning-plan proposal. Check the ID and try again.",
        )
        return
    if proposal["status"] != "pending_review":
        await _finish(
            conn,
            operation_id,
            f"PLAN_PROPOSAL_ALREADY_{proposal['status'].upper()}",
            f"That proposal was already {proposal['status']}. No further changes were made.",
        )
        return

    if not approve:
        await conn.execute(
            "UPDATE learning_plan_proposals SET status='rejected',decided_at=now() WHERE proposal_id=$1",
            proposal_id,
        )
        await _finish(
            conn,
            operation_id,
            "LEARNING_PLAN_REJECTED",
            "Proposal rejected. Your active learning plan was not changed.",
        )
        return

    plan = proposal["content"]
    if isinstance(plan, str):
        plan = json.loads(plan)
    learner_context = proposal["learner_context"]
    if isinstance(learner_context, str):
        learner_context = json.loads(learner_context)
    canonical_proposal = json.dumps(
        {"goal": proposal["goal"], "context": learner_context, "plan": plan},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    actual_hash = hashlib.sha256(canonical_proposal.encode("utf-8")).hexdigest()
    if actual_hash != proposal["content_hash"]:
        await _finish(
            conn,
            operation_id,
            "PLAN_PROPOSAL_CONTENT_HASH_MISMATCH",
            "I couldn't verify that proposal's saved contents, so it was not activated.",
        )
        return
    errors = validate_learning_plan(
        plan,
        trusted_hosts=set(SOFTWARE_CS_TRUSTED_HOSTS),
        weekly_minutes=(
            learner_context.get("weekly_minutes")
            if isinstance(learner_context, dict)
            else None
        ),
    )
    if errors:
        await _finish(
            conn,
            operation_id,
            "PLAN_PROPOSAL_FAILED_VALIDATION",
            "That proposal no longer passes the deterministic safety checks, so it was not activated.",
        )
        return
    content = _curriculum_content(plan)
    curriculum, digest = canonical_content(Curriculum.model_validate(content))
    curriculum_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO curriculum_versions(curriculum_id,status,content_hash,content) VALUES($1,'draft',$2,$3::jsonb)",
        curriculum_id,
        digest,
        json.dumps(curriculum),
    )
    old_content = None
    if state["active_curriculum_id"] is not None:
        old_row = await conn.fetchrow(
            "SELECT content,content_hash FROM curriculum_versions WHERE curriculum_id=$1",
            state["active_curriculum_id"],
        )
        if old_row is None:
            raise ValueError("active curriculum reference is missing")
        old_content = old_row["content"]
        if isinstance(old_content, str):
            old_content = json.loads(old_content)
        _, old_digest = canonical_content(Curriculum.model_validate(old_content))
        if old_digest != old_row["content_hash"]:
            raise ValueError("active curriculum content hash mismatch")

    progress_rows = await conn.fetch("SELECT topic_id,completed FROM topic_progress")
    progress = {row["topic_id"]: row["completed"] for row in progress_rows}
    impact = curriculum_impact(old_content, curriculum, progress, [])
    carried = set(impact["completion_carryover_topic_ids"])
    await conn.execute(
        "UPDATE curriculum_versions SET status='approved',approved_at=now() WHERE curriculum_id=$1",
        curriculum_id,
    )
    for topic in curriculum["topics"]:
        await conn.execute(
            """INSERT INTO topic_progress(topic_id,completed,study_minutes)
               VALUES($1,$2,0) ON CONFLICT(topic_id) DO UPDATE SET completed=$2""",
            topic["topic_id"],
            topic["topic_id"] in carried,
        )
    await conn.execute(
        "UPDATE study_days SET state='replan_required',queue_revision=queue_revision+1 WHERE state='valid'"
    )
    await conn.execute(
        "UPDATE engine_state SET active_curriculum_id=$1,state_revision=state_revision+1,progress_revision=progress_revision+1 WHERE singleton=true",
        curriculum_id,
    )
    await conn.execute(
        "UPDATE learning_plan_proposals SET status='approved',curriculum_id=$2,decided_at=now() WHERE proposal_id=$1",
        proposal_id,
        curriculum_id,
    )
    await _finish(
        conn,
        operation_id,
        "LEARNING_PLAN_APPROVED",
        f"Proposal approved and activated as curriculum {curriculum_id}. Use /today to create today's study queue.",
        extra={"proposal_id": str(proposal_id), "curriculum_id": str(curriculum_id)},
    )


def _curriculum_content(plan: dict) -> dict:
    topics = []
    for position, topic in enumerate(plan["topics"]):
        topics.append(
            {
                "topic_id": topic["topic_id"],
                "title": topic["title"],
                "description": topic["description"],
                "estimate_minutes": topic["estimate_minutes"],
                "position": position,
                "prerequisites": topic["prerequisites"],
                "resources": [
                    {"url": resource["url"], "title": resource["title"]}
                    for resource in topic["resources"]
                ],
            }
        )
    return {"topics": topics}


async def _finish(
    conn,
    operation_id: uuid.UUID,
    result_code: str,
    message: str,
    *,
    extra: dict | None = None,
):
    result = {"code": result_code, **(extra or {})}
    await conn.execute(
        """UPDATE operations SET status='completed',
           result=jsonb_set(jsonb_set(result,'{status}','\"completed\"'::jsonb),
           '{result}',$2::jsonb) WHERE operation_id=$1""",
        operation_id,
        json.dumps(result),
    )
    await conn.execute(
        """INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload)
           VALUES($1,$2,$3,'plan_decision',jsonb_build_object('text',$4::text))
           ON CONFLICT(logical_key) DO NOTHING""",
        uuid.uuid4(),
        f"operation:{operation_id}:plan-decision",
        operation_id,
        message,
    )
