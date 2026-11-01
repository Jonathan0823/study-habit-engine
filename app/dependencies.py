import hmac

import asyncpg
from fastapi import Header, HTTPException

_pool: asyncpg.Pool | None = None


def set_pool(pool: asyncpg.Pool | None) -> None:
    global _pool
    _pool = pool


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database is unavailable")
    return _pool


def required_config(name: str) -> str:
    import os

    value = os.getenv(name)
    if not value:
        raise HTTPException(
            503, detail={"code": "CONFIGURATION_MISSING", "retryable": False}
        )
    return value


def admin_auth(authorization: str | None = Header(default=None)) -> None:
    expected = required_config("ADMIN_SERVICE_TOKEN")
    if authorization is None or not hmac.compare_digest(
        authorization, f"Bearer {expected}"
    ):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "retryable": False})
