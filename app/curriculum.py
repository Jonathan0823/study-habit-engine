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


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping: list[dict] = Field(default_factory=list)


@router.post("/{version}/preview", dependencies=[Depends(admin_auth)])
async def preview_curriculum(
    version: uuid.UUID,
    body: PreviewRequest,
    idempotency_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
):
    if body.mapping:
        raise HTTPException(
            422,
            detail={
                "code": "MAPPING_NOT_SUPPORTED_BEFORE_FIRST_ACTIVATION",
                "retryable": False,
            },
        )
    request_hash = hashlib.sha256(
        json.dumps(
            {"version": str(version), "mapping": body.mapping},
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
        if state["active_curriculum_id"] is not None:
            raise HTTPException(
                422, detail={"code": "MAPPING_REQUIRED", "retryable": False}
            )
        content, digest = verify_stored_content(row)
        await conn.execute(
            "INSERT INTO curriculum_previews(token,curriculum_id,content_hash,state_revision,progress_revision) VALUES($1,$2,$3,$4,$5)",
            token,
            version,
            digest,
            state["state_revision"],
            state["progress_revision"],
        )
        result = {
            "confirmation_token": token,
            "version": str(version),
            "content_hash": digest,
            "topics": len(content["topics"]),
            "new_topic_ids": [t["topic_id"] for t in content["topics"]],
            "removed_topic_ids": [],
            "state_revision": state["state_revision"],
            "progress_revision": state["progress_revision"],
            "queue_invalidation": False,
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
            state = await conn.fetchrow(
                "SELECT * FROM engine_state WHERE singleton=true FOR UPDATE"
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
            if (
                preview["state_revision"] != state["state_revision"]
                or preview["progress_revision"] != state["progress_revision"]
                or preview["content_hash"] != stored_hash
                or version["status"] != "draft"
                or state["active_curriculum_id"] is not None
            ):
                raise HTTPException(
                    409, detail={"code": "STALE_PREVIEW", "retryable": False}
                )
            await conn.execute(
                "UPDATE curriculum_versions SET status='approved',approved_at=now() WHERE curriculum_id=$1",
                preview["curriculum_id"],
            )
            await conn.execute(
                "UPDATE engine_state SET active_curriculum_id=$1,state_revision=state_revision+1 WHERE singleton=true",
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
