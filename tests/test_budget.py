import asyncio

import pytest

from app.main import lock_replan_state, lock_study_date_requests, parse_budget


@pytest.mark.parametrize("value", ["0", "45", "90"])
def test_budget_accepts_whole_minutes_within_bounds(value):
    assert parse_budget([value]) == (int(value), None)


@pytest.mark.parametrize(
    "arguments", [[], ["-1"], ["91"], ["1.5"], ["+1"], ["１２"], ["30", "40"]]
)
def test_budget_rejects_missing_non_integer_or_out_of_range(arguments):
    value, error = parse_budget(arguments)
    assert value is None
    assert error in {"BUDGET_PROMPT", "INVALID_BUDGET"}


def test_queue_requests_use_a_date_scoped_transaction_lock():
    class RecordingConnection:
        def __init__(self):
            self.statement = None

        async def execute(self, query, *_):
            self.statement = query

    conn = RecordingConnection()
    asyncio.run(lock_study_date_requests(conn, "2026-11-01"))
    assert "pg_advisory_xact_lock" in conn.statement
    assert "DATE '2000-01-01'" in conn.statement
    assert "FOR UPDATE" not in conn.statement


def test_budget_mutation_locks_jobs_before_day_update():
    class RecordingConnection:
        def __init__(self):
            self.statements = []

        async def fetch(self, query, *_):
            self.statements.append(query)
            return []

        async def fetchrow(self, query, *_):
            self.statements.append(query)
            return {"requested_generation": 0}

        async def execute(self, query, *_):
            self.statements.append(query)

    conn = RecordingConnection()
    asyncio.run(lock_replan_state(conn, "2026-11-01", budget_value=45))
    assert "planning_jobs" in conn.statements[0] and "FOR UPDATE" in conn.statements[0]
    assert "engine_state" in conn.statements[1] and "FOR UPDATE" in conn.statements[1]
    assert "INSERT INTO study_days" in conn.statements[2]
    assert "study_days" in conn.statements[3] and "FOR UPDATE" in conn.statements[3]
    assert "UPDATE study_days SET budget_minutes" in conn.statements[4]
