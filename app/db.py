from pathlib import Path

import asyncpg

SCHEMA = Path(__file__).with_name("schema.sql")


async def apply_schema(database_url: str) -> None:
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute(SCHEMA.read_text())
    finally:
        await connection.close()
