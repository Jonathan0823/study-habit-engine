import asyncio
import os
import re

import asyncpg

from app.db import apply_schema


async def migrate() -> None:
    database_url = os.environ["MIGRATION_DATABASE_URL"]
    runtime_role = os.environ["RUNTIME_DATABASE_ROLE"]
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", runtime_role):
        raise ValueError("invalid runtime role name")

    await apply_schema(database_url)
    connection = await asyncpg.connect(database_url)
    try:
        database = await connection.fetchval("SELECT current_database()")
        privileges = "SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER"
        await connection.execute(
            f'GRANT CONNECT ON DATABASE "{database}" TO "{runtime_role}"'
        )
        await connection.execute(f'GRANT USAGE ON SCHEMA public TO "{runtime_role}"')
        await connection.execute(
            f'GRANT {privileges} ON ALL TABLES IN SCHEMA public TO "{runtime_role}"'
        )
        await connection.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {privileges} ON TABLES TO "{runtime_role}"'
        )
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(migrate())
