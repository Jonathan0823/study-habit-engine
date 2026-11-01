import hashlib
import json
import secrets
import uuid
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.dependencies import admin_auth, get_pool, required_config

router = APIRouter(prefix="/v1/admin/curricula")


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=300)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resource title must not be blank")
        return value

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("invalid resource URL") from exc
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or any(char.isspace() for char in value)
            or parsed.username
            or parsed.password
        ):
            raise ValueError("resource URL must be http(s) without credentials")
        return value


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=5000)
    estimate_minutes: int = Field(gt=0)
    position: int = Field(ge=0)
    prerequisites: list[str]
    resources: list[Resource] = Field(min_length=1)

    @field_validator("title", "description")
    @classmethod
    def nonblank_display_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic display text must not be blank")
        return value


class Curriculum(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: list[Topic] = Field(min_length=1)

    @field_validator("topics")
    @classmethod
    def valid_graph(cls, topics: list[Topic]) -> list[Topic]:
        ids = [topic.topic_id for topic in topics]
        positions = [topic.position for topic in topics]
        known = set(ids)
        if len(known) != len(ids) or len(set(positions)) != len(ids):
            raise ValueError("topic IDs and positions must be unique")
        for topic in topics:
            if len(set(topic.prerequisites)) != len(topic.prerequisites):
                raise ValueError("duplicate prerequisite")
            if (
                topic.topic_id in topic.prerequisites
                or not set(topic.prerequisites) <= known
            ):
                raise ValueError("invalid prerequisite reference")
        graph = {topic.topic_id: topic.prerequisites for topic in topics}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(topic_id: str) -> None:
            if topic_id in visiting:
                raise ValueError("prerequisite cycle")
            if topic_id in visited:
                return
            visiting.add(topic_id)
            for prerequisite in graph[topic_id]:
                visit(prerequisite)
            visiting.remove(topic_id)
            visited.add(topic_id)

        for topic_id in ids:
            visit(topic_id)
        return topics


def canonical_content(curriculum: Curriculum) -> tuple[dict, str]:
    content = curriculum.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return content, digest


def admin_actor_hash() -> str:
    return hashlib.sha256(required_config("ADMIN_SERVICE_TOKEN").encode()).hexdigest()


async def replay_idempotency(
    conn, actor_hash: str, key: uuid.UUID, method: str, request_hash: str
):
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"{actor_hash}:{key}",
    )
    saved = await conn.fetchrow(
        "SELECT request_method,request_hash,result FROM admin_idempotency_operations WHERE actor_hash=$1 AND idempotency_key=$2",
        actor_hash,
        key,
    )
    if saved is None:
        return None
    if saved["request_method"] != method or saved["request_hash"] != request_hash:
        raise HTTPException(
            409, detail={"code": "IDEMPOTENCY_CONFLICT", "retryable": False}
        )
    return (
        json.loads(saved["result"])
        if isinstance(saved["result"], str)
        else saved["result"]
    )


async def save_idempotency(
    conn, actor_hash: str, key: uuid.UUID, method: str, request_hash: str, result: dict
):
    await conn.execute(
        "INSERT INTO admin_idempotency_operations(actor_hash,idempotency_key,request_method,request_hash,result) VALUES($1,$2,$3,$4,$5::jsonb)",
        actor_hash,
        key,
        method,
        request_hash,
        json.dumps(result),
    )


def verify_stored_content(row) -> tuple[dict, str]:
    content = (
        row["content"]
        if isinstance(row["content"], dict)
        else json.loads(row["content"])
    )
    try:
        canonical, digest = canonical_content(Curriculum.model_validate(content))
    except Exception as exc:
        raise HTTPException(
            409, detail={"code": "CURRICULUM_CONTENT_CORRUPT", "retryable": False}
        ) from exc
    if digest != row["content_hash"]:
        raise HTTPException(
            409, detail={"code": "CURRICULUM_HASH_MISMATCH", "retryable": False}
        )
    return canonical, digest


@router.post("/import", dependencies=[Depends(admin_auth)])
async def import_curriculum(
    body: Curriculum,
    idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
):
    content, digest = canonical_content(body)
    request_hash = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    actor_hash = admin_actor_hash()
    curriculum_id = uuid.uuid4()
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        replay = await replay_idempotency(
            conn, actor_hash, idempotency_key, "import", request_hash
        )
        if replay is not None:
            return replay
        await conn.execute(
            "INSERT INTO curriculum_versions(curriculum_id,status,content_hash,content) VALUES($1,'draft',$2,$3::jsonb)",
            curriculum_id,
            digest,
            json.dumps(content),
        )
        result = {
            "version": str(curriculum_id),
            "status": "draft",
            "content_hash": digest,
        }
        await save_idempotency(
            conn, actor_hash, idempotency_key, "import", request_hash, result
        )
        return result


@router.post("/{version}/validate", dependencies=[Depends(admin_auth)])
async def validate_curriculum(version: uuid.UUID):
    db = await get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content_hash,content,status FROM curriculum_versions WHERE curriculum_id=$1",
            version,
        )
    if row is None:
        raise HTTPException(
            404, detail={"code": "CURRICULUM_NOT_FOUND", "retryable": False}
        )
    _, digest = verify_stored_content(row)
    return {
        "version": str(version),
        "status": row["status"],
        "content_hash": digest,
        "valid": True,
    }


class CompletionMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_topic_ids: list[str] = Field(min_length=1)
    to_topic_id: str
    carry_completion: bool

    @field_validator("from_topic_ids")
    @classmethod
    def unique_sources(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("mapping source IDs must be unique")
        return value


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping: list[CompletionMapping] = Field(default_factory=list)


def curriculum_impact(
    old: dict | None,
    new: dict,
    progress: dict[str, bool],
    mappings: list[CompletionMapping],
) -> dict:
    old_topics = {item["topic_id"]: item for item in (old or {}).get("topics", [])}
    new_topics = {item["topic_id"]: item for item in new["topics"]}
    unchanged = {
        key
        for key in old_topics.keys() & new_topics.keys()
        if old_topics[key]["description"] == new_topics[key]["description"]
        and old_topics[key]["prerequisites"] == new_topics[key]["prerequisites"]
    }
    changed_objectives = {
        key
        for key in old_topics.keys() & new_topics.keys()
        if old_topics[key]["description"] != new_topics[key]["description"]
    }
    changed_prerequisites = {
        key
        for key in old_topics.keys() & new_topics.keys()
        if old_topics[key]["prerequisites"] != new_topics[key]["prerequisites"]
    }
    removed = (set(old_topics) - set(new_topics)) | changed_objectives
    mapped_sources: set[str] = set()
    mapped_targets: dict[str, bool] = {}
    for mapping in mappings:
        sources = set(mapping.from_topic_ids)
        if sources & changed_objectives & set(new_topics):
            raise ValueError(
                "mapped source IDs with changed objectives must be retired from the revised curriculum"
            )
        if mapping.to_topic_id in old_topics:
            raise ValueError(
                "completion mappings must target an identity absent from the prior curriculum"
            )
        if (
            not sources <= removed
            or mapping.to_topic_id not in new_topics
            or (sources & changed_objectives and mapping.to_topic_id in sources)
        ):
            raise ValueError(
                "changed objectives require an explicit mapping to a new identity"
            )
        if mapped_sources & sources or mapping.to_topic_id in mapped_targets:
            raise ValueError("mapping sources and targets must be unique")
        mapped_sources |= sources
        mapped_targets[mapping.to_topic_id] = mapping.carry_completion
    if changed_objectives - mapped_sources:
        raise ValueError("changed objectives require explicit completion mapping")
    completion = {topic_id for topic_id in unchanged if progress.get(topic_id, False)}
    for target, carry in mapped_targets.items():
        if carry and any(
            progress.get(source, False)
            for source in next(
                item.from_topic_ids for item in mappings if item.to_topic_id == target
            )
        ):
            completion.add(target)
    # Carryover is reviewed, but cannot create a completion with unmet target prerequisites.
    by_id = new_topics
    # A topic whose prerequisites changed is a new learning context even when its
    # objective text is unchanged; do not silently carry its completion.
    invalidated = {
        topic_id for topic_id in changed_prerequisites if progress.get(topic_id, False)
    }
    changed = True
    while changed:
        changed = False
        for topic_id in tuple(completion):
            if not set(by_id[topic_id]["prerequisites"]) <= completion:
                completion.remove(topic_id)
                invalidated.add(topic_id)
                changed = True
    # Report the full downstream closure, not just topics directly referencing a
    # changed/removed prerequisite. Include only completed dependents whose
    # completion activation will clear.
    affected = set(removed | invalidated)
    dependents = set()
    changed = True
    while changed:
        changed = False
        for topic_id, topic in new_topics.items():
            if topic_id in affected:
                continue
            if set(topic["prerequisites"]) & affected:
                affected.add(topic_id)
                if progress.get(topic_id, False):
                    dependents.add(topic_id)
                changed = True
    return {
        "retained_topic_ids": sorted(unchanged),
        "new_topic_ids": sorted(
            (set(new_topics) - set(old_topics)) | changed_objectives
        ),
        "removed_topic_ids": sorted(removed),
        "mapped_topic_ids": [
            {
                "from_topic_ids": sorted(item.from_topic_ids),
                "to_topic_id": item.to_topic_id,
                "carry_completion": item.carry_completion,
            }
            for item in mappings
        ],
        "completion_carryover_topic_ids": sorted(completion),
        "prerequisite_invalidated_topic_ids": sorted(invalidated | dependents),
    }


@router.post("/{version}/preview", dependencies=[Depends(admin_auth)])
async def preview_curriculum(
    version: uuid.UUID,
    body: PreviewRequest,
    idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
):
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "version": str(version),
                "mapping": [item.model_dump() for item in body.mapping],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    actor_hash = admin_actor_hash()
    db = await get_pool()
    token = secrets.token_urlsafe(32)
    async with db.acquire() as conn, conn.transaction():
        replay = await replay_idempotency(
            conn, actor_hash, idempotency_key, "preview", request_hash
        )
        if replay is not None:
            return replay
        state = await conn.fetchrow(
            "SELECT state_revision,progress_revision,active_curriculum_id FROM engine_state WHERE singleton=true FOR UPDATE"
        )
        row = await conn.fetchrow(
            "SELECT status,content_hash,content FROM curriculum_versions WHERE curriculum_id=$1",
            version,
        )
        if row is None:
            raise HTTPException(
                404, detail={"code": "CURRICULUM_NOT_FOUND", "retryable": False}
            )
        if row["status"] != "draft":
            raise HTTPException(
                409, detail={"code": "CURRICULUM_ALREADY_APPROVED", "retryable": False}
            )
        content, digest = verify_stored_content(row)
        old_content = None
        if state["active_curriculum_id"] is not None:
            previous = await conn.fetchrow(
                "SELECT content FROM curriculum_versions WHERE curriculum_id=$1",
                state["active_curriculum_id"],
            )
            old_content = (
                previous["content"]
                if isinstance(previous["content"], dict)
                else json.loads(previous["content"])
            )
        progress_rows = await conn.fetch(
            "SELECT topic_id,completed FROM topic_progress"
        )
        progress = {item["topic_id"]: item["completed"] for item in progress_rows}
        try:
            impact = curriculum_impact(old_content, content, progress, body.mapping)
        except ValueError as exc:
            raise HTTPException(
                422,
                detail={
                    "code": "INVALID_COMPLETION_MAPPING",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        # Snapshot every existing queue revision, including non-actionable states:
        # any change after review makes the confirmation stale.
        queues = await conn.fetch(
            "SELECT study_date,queue_revision,state FROM study_days ORDER BY study_date"
        )
        impact["queue_impact"] = [
            {
                "study_date": str(day["study_date"]),
                "queue_revision": day["queue_revision"],
                "state": day["state"],
                "will_invalidate": day["state"] == "valid",
            }
            for day in queues
        ]
        await conn.execute(
            "INSERT INTO curriculum_previews(token,curriculum_id,content_hash,state_revision,progress_revision,queue_snapshot,mapping) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb)",
            token,
            version,
            digest,
            state["state_revision"],
            state["progress_revision"],
            json.dumps(
                [
                    {
                        "study_date": str(day["study_date"]),
                        "queue_revision": day["queue_revision"],
                    }
                    for day in queues
                ]
            ),
            json.dumps([item.model_dump() for item in body.mapping]),
        )
        result = {
            "confirmation_token": token,
            "version": str(version),
            "content_hash": digest,
            "topics": len(content["topics"]),
            **impact,
            "state_revision": state["state_revision"],
            "progress_revision": state["progress_revision"],
            "queue_invalidation": any(day["state"] == "valid" for day in queues),
        }
        await save_idempotency(
            conn, actor_hash, idempotency_key, "preview", request_hash, result
        )
        return result


@router.get("/active", dependencies=[Depends(admin_auth)])
async def active_curriculum():
    db = await get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT v.curriculum_id,v.status,v.content_hash,v.content FROM engine_state s LEFT JOIN curriculum_versions v ON v.curriculum_id=s.active_curriculum_id WHERE s.singleton=true"
        )
    if row is None or row["curriculum_id"] is None:
        return {"active": None}
    content, digest = verify_stored_content(row)
    return {
        "active": {
            "version": str(row["curriculum_id"]),
            "status": row["status"],
            "content_hash": digest,
            "curriculum": content,
        }
    }


def router_confirmation():
    from fastapi import APIRouter

    confirm = APIRouter(prefix="/v1/admin/confirmations")

    @confirm.post("/{token}", dependencies=[Depends(admin_auth)])
    async def activate(
        token: str,
        idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    ):
        request_hash = hashlib.sha256(
            json.dumps({"token": token}, sort_keys=True).encode()
        ).hexdigest()
        db = await get_pool()
        actor_hash = admin_actor_hash()
        async with db.acquire() as conn, conn.transaction():
            replay = await replay_idempotency(
                conn, actor_hash, idempotency_key, "activate", request_hash
            )
            if replay is not None:
                return replay
            # Match queue writers' lock order: date advisory locks, active jobs,
            # engine state, then study days. Holding state prevents day creation or
            # queue revision updates until this transaction commits.
            day_dates = await conn.fetch(
                "SELECT study_date FROM study_days ORDER BY study_date"
            )
            for day in day_dates:
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
            await conn.fetch(
                "SELECT study_date FROM study_days ORDER BY study_date FOR UPDATE"
            )
            preview = await conn.fetchrow(
                "SELECT * FROM curriculum_previews WHERE token=$1 FOR UPDATE", token
            )
            if preview is None:
                raise HTTPException(
                    404, detail={"code": "PREVIEW_NOT_FOUND", "retryable": False}
                )
            version = await conn.fetchrow(
                "SELECT * FROM curriculum_versions WHERE curriculum_id=$1 FOR UPDATE",
                preview["curriculum_id"],
            )
            if preview["consumed_at"] is not None:
                if state["active_curriculum_id"] == preview["curriculum_id"]:
                    result = {
                        "status": "approved",
                        "version": str(preview["curriculum_id"]),
                        "content_hash": preview["content_hash"],
                    }
                    await save_idempotency(
                        conn,
                        actor_hash,
                        idempotency_key,
                        "activate",
                        request_hash,
                        result,
                    )
                    return result
                raise HTTPException(
                    409, detail={"code": "PREVIEW_ALREADY_CONSUMED", "retryable": False}
                )
            _, stored_hash = verify_stored_content(version)
            current_queues = await conn.fetch(
                "SELECT study_date,queue_revision FROM study_days ORDER BY study_date"
            )
            queued = (
                preview["queue_snapshot"]
                if isinstance(preview["queue_snapshot"], list)
                else json.loads(preview["queue_snapshot"])
            )
            current_queue_snapshot = [
                {
                    "study_date": str(day["study_date"]),
                    "queue_revision": day["queue_revision"],
                }
                for day in current_queues
            ]
            if (
                preview["state_revision"] != state["state_revision"]
                or preview["progress_revision"] != state["progress_revision"]
                or queued != current_queue_snapshot
                or preview["content_hash"] != stored_hash
                or version["status"] != "draft"
            ):
                raise HTTPException(
                    409, detail={"code": "STALE_PREVIEW", "retryable": False}
                )
            await conn.execute(
                "UPDATE curriculum_versions SET status='approved',approved_at=now() WHERE curriculum_id=$1",
                preview["curriculum_id"],
            )
            mapping = (
                preview["mapping"]
                if isinstance(preview["mapping"], list)
                else json.loads(preview["mapping"])
            )
            old_content = None
            if state["active_curriculum_id"] is not None:
                old_row = await conn.fetchrow(
                    "SELECT content FROM curriculum_versions WHERE curriculum_id=$1",
                    state["active_curriculum_id"],
                )
                old_content = (
                    old_row["content"]
                    if isinstance(old_row["content"], dict)
                    else json.loads(old_row["content"])
                )
            current_content, _ = verify_stored_content(version)
            progress_rows = await conn.fetch(
                "SELECT topic_id,completed,study_minutes FROM topic_progress"
            )
            progress = {item["topic_id"]: item["completed"] for item in progress_rows}
            impact = curriculum_impact(
                old_content,
                current_content,
                progress,
                [CompletionMapping.model_validate(item) for item in mapping],
            )
            carried = set(impact["completion_carryover_topic_ids"])
            for topic_id in {topic["topic_id"] for topic in current_content["topics"]}:
                await conn.execute(
                    "INSERT INTO topic_progress(topic_id,completed,study_minutes) VALUES($1,$2,0) ON CONFLICT(topic_id) DO UPDATE SET completed=$2",
                    topic_id,
                    topic_id in carried,
                )
            # Queues are retained for review but become nonactionable against the new grounding.
            await conn.execute(
                "UPDATE study_days SET state='replan_required',queue_revision=queue_revision+1 WHERE state='valid'"
            )
            await conn.execute(
                "UPDATE engine_state SET active_curriculum_id=$1,state_revision=state_revision+1,progress_revision=progress_revision+1 WHERE singleton=true",
                preview["curriculum_id"],
            )
            await conn.execute(
                "UPDATE curriculum_previews SET consumed_at=now() WHERE token=$1", token
            )
            result = {
                "status": "approved",
                "version": str(preview["curriculum_id"]),
                "content_hash": stored_hash,
            }
            await save_idempotency(
                conn, actor_hash, idempotency_key, "activate", request_hash, result
            )
            return result

    return confirm


confirmation_router = router_confirmation()
