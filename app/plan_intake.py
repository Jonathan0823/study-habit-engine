"""Durable Telegram intake for learner goals and weekly study time."""

from __future__ import annotations

import json
import uuid


def parse_weekly_minutes(text: str) -> int | None:
    value = text.strip()
    if not value.isascii() or not value.isdecimal():
        return None
    minutes = int(value)
    return minutes if 1 <= minutes <= 10080 else None


async def begin_learning_plan(
    conn,
    operation_id: uuid.UUID,
    actor_id: int,
    chat_id: int,
    initial_goal: str = "",
):
    goal_too_long = bool(initial_goal) and len(initial_goal) > 2000
    goal = initial_goal if initial_goal and not goal_too_long else None
    phase = "awaiting_weekly_minutes" if goal else "awaiting_goal"
    await conn.execute(
        """INSERT INTO learning_plan_sessions
           (actor_id,chat_id,started_by_operation_id,phase,goal,expires_at)
           VALUES($1,$2,$3,$4,$5,now()+interval '30 minutes')
           ON CONFLICT(actor_id,chat_id) DO UPDATE SET
             started_by_operation_id=EXCLUDED.started_by_operation_id,
             phase=EXCLUDED.phase,goal=EXCLUDED.goal,
             expires_at=EXCLUDED.expires_at,updated_at=now()""",
        actor_id,
        chat_id,
        operation_id,
        phase,
        goal,
    )
    prompt = (
        "Send your learning goal in one message (up to 2,000 characters)."
        if goal_too_long
        else "How many whole minutes can you study per week? Reply with a number from 1 to 10080 (for example, 300)."
        if goal
        else "Currently /learn supports programming and computer science only. What do you want to learn in that area? Include what you already know, and any deadline or language preference that matters."
    )
    await _deliver(
        conn,
        operation_id,
        f"operation:{operation_id}:learn-start",
        prompt,
    )
    if goal_too_long:
        await _complete_operation(conn, operation_id, "INVALID_LEARNING_GOAL")


async def accept_learning_plan_text(
    conn,
    operation_id: uuid.UUID,
    actor_id: int,
    chat_id: int,
    text: str,
):
    session = await conn.fetchrow(
        """SELECT * FROM learning_plan_sessions
           WHERE actor_id=$1 AND chat_id=$2 AND expires_at>now() FOR UPDATE""",
        actor_id,
        chat_id,
    )
    if session is None:
        await _deliver(
            conn,
            operation_id,
            f"operation:{operation_id}:learn-no-session",
            "There is no active learning-plan setup. Send /learn to start one.",
        )
        await _complete_operation(conn, operation_id, "NO_ACTIVE_LEARNING_SESSION")
        return

    answer = text.strip()
    if session["phase"] == "awaiting_goal":
        if not answer or len(answer) > 2000:
            await _deliver(
                conn,
                operation_id,
                f"operation:{operation_id}:learn-invalid-goal",
                "Send your learning goal in one message (up to 2,000 characters).",
            )
            await _complete_operation(conn, operation_id, "INVALID_LEARNING_GOAL")
            return
        await conn.execute(
            """UPDATE learning_plan_sessions
               SET phase='awaiting_weekly_minutes',goal=$3,
                   expires_at=now()+interval '30 minutes',updated_at=now()
               WHERE actor_id=$1 AND chat_id=$2""",
            actor_id,
            chat_id,
            answer,
        )
        await _deliver(
            conn,
            operation_id,
            f"operation:{operation_id}:learn-weekly-budget",
            "How many whole minutes can you study per week? Reply with a number from 1 to 10080 (for example, 300).",
        )
        await _complete_operation(conn, operation_id, "LEARNING_PLAN_GOAL_RECEIVED")
        return

    weekly_minutes = parse_weekly_minutes(answer)
    if weekly_minutes is None:
        await _deliver(
            conn,
            operation_id,
            f"operation:{operation_id}:learn-invalid-weekly-budget",
            "Please reply with a whole number of minutes from 1 to 10080 (for example, 300).",
        )
        await _complete_operation(conn, operation_id, "INVALID_WEEKLY_BUDGET")
        return

    job_id = uuid.uuid4()
    context = {"weekly_minutes": weekly_minutes}
    await conn.execute(
        """INSERT INTO learning_plan_jobs(job_id,operation_id,goal,learner_context)
           VALUES($1,$2,$3,$4::jsonb)""",
        job_id,
        operation_id,
        session["goal"],
        json.dumps(context),
    )
    await conn.execute(
        "DELETE FROM learning_plan_sessions WHERE actor_id=$1 AND chat_id=$2",
        actor_id,
        chat_id,
    )
    await _deliver(
        conn,
        operation_id,
        f"operation:{operation_id}:learn-queued",
        "Thanks. I’ll prepare a sourced learning-plan draft for you to review. Your current learning plan will not change unless you approve it.",
    )
    await _complete_operation(conn, operation_id, "LEARNING_PLAN_QUEUED")


async def _deliver(conn, operation_id: uuid.UUID, logical_key: str, text: str):
    await conn.execute(
        """INSERT INTO deliveries(delivery_id,logical_key,operation_id,kind,payload)
           VALUES($1,$2,$3,'plan_intake',jsonb_build_object('text',$4::text))
           ON CONFLICT(logical_key) DO NOTHING""",
        uuid.uuid4(),
        logical_key,
        operation_id,
        text,
    )


async def _complete_operation(conn, operation_id: uuid.UUID, result_code: str):
    await conn.execute(
        """UPDATE operations
           SET status='completed',
               result=jsonb_set(
                   jsonb_set(result,'{status}','\"completed\"'::jsonb),
                   '{result}',jsonb_build_object('code',$2::text)
               )
           WHERE operation_id=$1""",
        operation_id,
        result_code,
    )
