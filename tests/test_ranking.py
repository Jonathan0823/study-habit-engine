import asyncio

import pytest

from app.ranking import rank_or_fallback, validate_ranking


def candidate(topic_id, *, position, started=0, prerequisites=(), completed=()):
    return {
        "topic_id": topic_id,
        "position": position,
        "estimate_minutes": 30,
        "prerequisites": list(prerequisites),
        "completed_prerequisites": list(completed),
        "started_minutes": started,
    }


def test_valid_ranking_returns_all_candidate_ids_in_model_order():
    candidates = [
        candidate("fresh", position=0),
        candidate("started", position=1, started=10),
        candidate("dependent", position=2, prerequisites=("root",), completed=("root",)),
    ]
    response = {
        "ranked_topics": [
            {"topic_id": "started", "reason_code": "CONTINUE_STARTED"},
            {"topic_id": "dependent", "reason_code": "PREREQUISITES_COMPLETE"},
            {"topic_id": "fresh", "reason_code": "NO_PREREQUISITES"},
        ]
    }

    assert validate_ranking(candidates, response) == ["started", "dependent", "fresh"]


@pytest.mark.parametrize(
    "response",
    [
        None,
        "not JSON",
        {"ranked_topics": [{"topic_id": "a", "reason_code": "NO_PREREQUISITES"}]},
        {
            "ranked_topics": [
                {"topic_id": "a", "reason_code": "NO_PREREQUISITES"},
                {"topic_id": "a", "reason_code": "NO_PREREQUISITES"},
            ]
        },
        {
            "ranked_topics": [
                {"topic_id": "a", "reason_code": "NO_PREREQUISITES"},
                {"topic_id": "unknown", "reason_code": "NO_PREREQUISITES"},
            ]
        },
        {
            "ranked_topics": [
                {"topic_id": "a", "reason_code": "INVENTED_REASON"},
                {"topic_id": "b", "reason_code": "NO_PREREQUISITES"},
            ]
        },
        {
            "ranked_topics": [
                {"topic_id": "a", "reason_code": "PREREQUISITES_COMPLETE"},
                {"topic_id": "b", "reason_code": "NO_PREREQUISITES"},
            ]
        },
        {
            "ranked_topics": [
                {"topic_id": "a", "reason_code": "NO_PREREQUISITES", "minutes": 30},
                {"topic_id": "b", "reason_code": "NO_PREREQUISITES"},
            ]
        },
    ],
)
def test_invalid_ranking_is_rejected_as_a_whole(response):
    candidates = [candidate("a", position=0), candidate("b", position=1)]
    assert validate_ranking(candidates, response) is None


def test_started_topics_must_precede_unstarted_topics():
    candidates = [
        candidate("fresh", position=0),
        candidate("started", position=1, started=5),
    ]
    response = {
        "ranked_topics": [
            {"topic_id": "fresh", "reason_code": "NO_PREREQUISITES"},
            {"topic_id": "started", "reason_code": "CONTINUE_STARTED"},
        ]
    }
    assert validate_ranking(candidates, response) is None


def test_valid_json_text_is_parsed_at_the_ranking_seam():
    candidates = [candidate("a", position=0)]
    response = '{"ranked_topics":[{"topic_id":"a","reason_code":"NO_PREREQUISITES"}]}'

    assert validate_ranking(candidates, response) == ["a"]


def test_ranker_is_called_once_and_can_change_order_within_started_group():
    candidates = [candidate("a", position=0), candidate("b", position=1)]
    calls = 0

    async def ranker(_candidates):
        nonlocal calls
        calls += 1
        return {"ranked_topics": [{"topic_id": "b"}, {"topic_id": "a"}]}

    assert asyncio.run(rank_or_fallback(candidates, ranker)) == ["b", "a"]
    assert calls == 1


def test_ranker_failure_uses_started_first_stable_curriculum_fallback():
    candidates = [
        candidate("new_later", position=2),
        candidate("started_later", position=3, started=1),
        candidate("new_first", position=0),
        candidate("started_first", position=1, started=1),
    ]

    async def ranker(_candidates):
        raise RuntimeError("provider unavailable")

    assert asyncio.run(rank_or_fallback(candidates, ranker)) == [
        "started_first",
        "started_later",
        "new_first",
        "new_later",
    ]


def test_budget_denial_uses_fallback_without_dispatching_ranker():
    candidates = [candidate("a", position=0)]

    async def forbidden_ranker(_candidates):
        raise AssertionError("must not dispatch when reservation is denied")

    assert asyncio.run(rank_or_fallback(candidates, None)) == ["a"]


def test_ranker_timeout_uses_fallback_after_a_single_dispatch():
    candidates = [candidate("a", position=0), candidate("b", position=1)]
    calls = 0

    async def ranker(_candidates):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"ranked_topics": [{"topic_id": "b"}, {"topic_id": "a"}]}

    assert asyncio.run(rank_or_fallback(candidates, ranker, timeout_seconds=0.001)) == ["a", "b"]
    assert calls == 1
