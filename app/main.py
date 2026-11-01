import hashlib
import hmac
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.commands import COMMANDS, help_text

pool: asyncpg.Pool | None = None


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["command"]
    command: str = Field(min_length=1, max_length=32)
    arguments: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list, max_length=8)


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: str = Field(pattern=r"^[0-9]{1,19}$")
    update_id: str = Field(pattern=r"^[0-9]{1,19}$")
    actor_id: str = Field(pattern=r"^[0-9]{1,19}$")
    chat_id: str = Field(pattern=r"^[0-9]{1,19}$")
    chat_type: Literal["private", "group", "supergroup", "channel"]
    poller_fence: int = Field(ge=1)
    payload: Payload


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: str
    owner_id: uuid.UUID


class DeliveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fence: int = Field(ge=1)
    outcome: Literal["sent", "transient_failure", "permanent_failure"]
    telegram_receipt: str | None = None


async def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("database is unavailable")
    return pool


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        pool = await asyncpg.create_pool(dsn)
    try:
        yield
    finally:
        if pool is not None:
            await pool.close()
            pool = None


app = FastAPI(lifespan=lifespan)


def required_config(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(503, detail={"code": "CONFIGURATION_MISSING", "retryable": False})
    return value


def service_auth(authorization: str | None = Header(default=None)) -> None:
    expected = required_config("BOT_SERVICE_TOKEN")
    if authorization is None or not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "retryable": False})


def fixed_error(status: int, code: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "retryable": retryable})


@app.exception_handler(asyncpg.PostgresError)
async def postgres_error(_: Request, __: asyncpg.PostgresError):
    return fixed_error(503, "DATABASE_UNAVAILABLE", True)


@app.exception_handler(RuntimeError)
async def service_error(_: Request, __: RuntimeError):
    return fixed_error(503, "DATABASE_UNAVAILABLE", True)


@app.get("/health/live")
async def health_live():
    return {"status": "live"}


@app.get("/health/ready")
async def health_ready():
    for name in ("BOT_SERVICE_TOKEN", "TELEGRAM_BOT_ID", "TELEGRAM_USER_ID", "TELEGRAM_PRIVATE_CHAT_ID"):
        required_config(name)
    db = await get_pool()
    async with db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ready"}


@app.post("/v1/telegram/claim", dependencies=[Depends(service_auth)])
async def claim(body: ClaimRequest):
    configured_bot = int(required_config("TELEGRAM_BOT_ID"))
    if int(body.bot_id) != configured_bot:
        raise HTTPException(403, detail={"code": "BOT_NOT_ALLOWED", "retryable": False})
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """INSERT INTO telegram_cursor(bot_id, next_offset, poller_owner, fence, lease_until)
               VALUES ($1, 0, $2, 1, now() + interval '60 seconds')
               ON CONFLICT (bot_id) DO UPDATE SET
                 fence = CASE WHEN telegram_cursor.poller_owner = EXCLUDED.poller_owner
                                   AND telegram_cursor.lease_until > now()
                              THEN telegram_cursor.fence ELSE telegram_cursor.fence + 1 END,
                 poller_owner = EXCLUDED.poller_owner,
                 lease_until = now() + interval '60 seconds'
               WHERE telegram_cursor.poller_owner = EXCLUDED.poller_owner
                  OR telegram_cursor.lease_until <= now()
               RETURNING next_offset, fence, lease_until""",
            configured_bot,
            body.owner_id,
        )
        if row is None:
            raise HTTPException(409, detail={"code": "POLLER_OWNED", "retryable": True})
        return {"next_offset": row["next_offset"], "fence": row["fence"], "lease_until": row["lease_until"].isoformat()}


@app.post("/v1/telegram/updates", dependencies=[Depends(service_auth)])
async def ingest(body: Update):
    try:
        bot_id, update_id, actor_id, chat_id = map(int, (body.bot_id, body.update_id, body.actor_id, body.chat_id))
    except ValueError:
        raise HTTPException(400, detail={"code": "INVALID_ID", "retryable": False})
    if min(bot_id, update_id, actor_id, chat_id) < 0 or max(bot_id, update_id, actor_id, chat_id) > 2**63 - 1:
        raise HTTPException(400, detail={"code": "INVALID_ID", "retryable": False})
    configured_bot = int(required_config("TELEGRAM_BOT_ID"))
    source = f"telegram:{bot_id}:{update_id}"
    semantic = {"actor_id": body.actor_id, "chat_id": body.chat_id, "chat_type": body.chat_type, "payload": body.payload.model_dump()}
    request_hash = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        duplicate = await conn.fetchrow(
            """SELECT o.operation_id, o.request_hash, o.result FROM telegram_ingestion i
               JOIN operations o USING (operation_id) WHERE i.bot_id=$1 AND i.update_id=$2""",
            bot_id, update_id,
        )
        if duplicate:
            if duplicate["request_hash"] != request_hash:
                raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT", "retryable": False})
            saved_result = duplicate["result"]
            return json.loads(saved_result) if isinstance(saved_result, str) else saved_result

        cursor = await conn.fetchrow("SELECT * FROM telegram_cursor WHERE bot_id=$1 FOR UPDATE", configured_bot)
        if (bot_id != configured_bot or cursor is None or cursor["fence"] != body.poller_fence
                or cursor["lease_until"] is None or cursor["lease_until"] <= await conn.fetchval("SELECT now()")):
            raise HTTPException(409, detail={"code": "STALE_POLLER", "retryable": True})
        if update_id < cursor["next_offset"]:
            raise HTTPException(409, detail={"code": "OUT_OF_ORDER_UPDATE", "retryable": True})

        authorized = (body.chat_type == "private" and actor_id == int(required_config("TELEGRAM_USER_ID"))
                      and chat_id == int(required_config("TELEGRAM_PRIVATE_CHAT_ID")) and actor_id == chat_id)
        defined_command = any(name == body.payload.command for name, _, _ in COMMANDS)
        implemented_command = body.payload.command == "help"
        operation_id = uuid.uuid4()
        status = "accepted" if authorized and implemented_command else "rejected"
        result_code = (
            "UNAUTHORIZED" if not authorized else
            "UNSUPPORTED_COMMAND" if not defined_command else
            "NOT_IMPLEMENTED" if not implemented_command else
            "HELP_READY"
        )
        result = {
            "operation_id": str(operation_id),
            "status": status,
            "result": {"code": result_code},
            "callback_notice": "Accepted" if status == "accepted" else None,
        }
        await conn.execute(
            "INSERT INTO operations(operation_id,source_key,request_hash,status,result) VALUES($1,$2,$3,$4,$5::jsonb)",
            operation_id, source, request_hash, status, json.dumps(result),
        )
        await conn.execute(
            "INSERT INTO telegram_ingestion(bot_id,update_id,operation_id,request_hash) VALUES($1,$2,$3,$4)",
            bot_id, update_id, operation_id, request_hash,
        )
        if status == "accepted":
            await conn.execute(
                """INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload)
                   VALUES($1,$2,$3,'help',jsonb_build_object('text',$4::text))""",
                uuid.uuid4(), f"operation:{operation_id}:help", operation_id, help_text(),
            )
        await conn.execute("UPDATE telegram_cursor SET next_offset=$2, lease_until=now()+interval '60 seconds' WHERE bot_id=$1", configured_bot, update_id + 1)
        return result


@app.post("/v1/deliveries/claim", dependencies=[Depends(service_auth)])
async def claim_delivery():
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE deliveries SET status='failed',lease_until=NULL WHERE status IN ('pending','leased') AND expires_at <= now()")
        row = await conn.fetchrow(
            """SELECT d.delivery_id,d.operation_id,d.payload,d.fence FROM deliveries d
               WHERE d.expires_at > now() AND (d.status='pending' AND d.next_attempt_at <= now()
                  OR d.status='leased' AND d.lease_until < now())
               ORDER BY d.created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
        )
        if row is None:
            return JSONResponse(status_code=204, content=None)
        leased = await conn.fetchrow(
            """UPDATE deliveries SET status='leased', attempts=attempts+1, fence=fence+1,
               lease_until=now()+interval '60 seconds' WHERE delivery_id=$1
               RETURNING fence,lease_until""", row["delivery_id"]
        )
        payload = json.loads(row["payload"])
        return {"delivery_id": str(row["delivery_id"]), "operation_id": str(row["operation_id"]),
                "fence": leased["fence"], "lease_until": leased["lease_until"].isoformat(),
                "chat_id": required_config("TELEGRAM_PRIVATE_CHAT_ID"), "text": payload["text"]}


@app.post("/v1/deliveries/{delivery_id}/result", dependencies=[Depends(service_auth)])
async def delivery_result(delivery_id: uuid.UUID, body: DeliveryResult):
    db = await get_pool()
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT status,fence,lease_until FROM deliveries WHERE delivery_id=$1 FOR UPDATE", delivery_id)
        if row is None:
            raise HTTPException(404, detail={"code": "DELIVERY_NOT_FOUND", "retryable": False})
        if row["status"] == "sent":
            return {"status": "sent"}
        if (row["status"] != "leased" or row["fence"] != body.fence
                or row["lease_until"] is None or row["lease_until"] <= await conn.fetchval("SELECT now()")):
            raise HTTPException(409, detail={"code": "STALE_DELIVERY", "retryable": True})
        if body.outcome == "sent":
            if body.telegram_receipt is None:
                raise HTTPException(400, detail={"code": "RECEIPT_REQUIRED", "retryable": False})
            status = "sent"
        elif body.outcome == "transient_failure":
            status = "pending"
        else:
            status = "failed"
        if body.outcome == "transient_failure":
            await conn.execute(
                """UPDATE deliveries SET status=CASE WHEN expires_at <= now() + make_interval(secs =>
                       LEAST(5 * power(2, LEAST(attempts,6)),300)::integer) THEN 'failed' ELSE 'pending' END,
                   next_attempt_at=now()+make_interval(secs => LEAST(5 * power(2, LEAST(attempts,6)),300)::integer),
                   lease_until=NULL WHERE delivery_id=$1""", delivery_id)
        else:
            await conn.execute("UPDATE deliveries SET status=$2,telegram_receipt=$3,lease_until=NULL WHERE delivery_id=$1",
                               delivery_id, status, body.telegram_receipt)
        return {"status": status}
