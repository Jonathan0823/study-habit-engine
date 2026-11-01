from app.planning import allocate


def topic(topic_id, position, started=0, prerequisites=(), completed=()):
    return {
        "topic_id": topic_id,
        "position": position,
        "estimate_minutes": 120,
        "prerequisites": list(prerequisites),
        "completed_prerequisites": list(completed),
        "started_minutes": started,
    }


def test_allocator_orders_started_first_and_allocates_one_per_pass():
    candidates = [topic("new", 0), topic("started", 1, started=4)]
    assert allocate(candidates, 90) == [
        {"topic_id": "started", "planned_minutes": 30},
        {"topic_id": "new", "planned_minutes": 30},
        {"topic_id": "started", "planned_minutes": 30},
    ]


def test_prerequisite_must_be_complete_now_not_just_in_queue():
    candidates = [topic("root", 0), topic("dependent", 1, prerequisites=("root",))]
    assert allocate(candidates, 60) == [
        {"topic_id": "root", "planned_minutes": 30},
        {"topic_id": "root", "planned_minutes": 30},
    ]


def test_partial_final_block_and_under_minimum_capacity():
    candidates = [topic("a", 0), topic("b", 1)]
    assert allocate(candidates, 45) == [
        {"topic_id": "a", "planned_minutes": 30},
        {"topic_id": "b", "planned_minutes": 15},
    ]
    assert allocate(candidates, 29) == [{"topic_id": "a", "planned_minutes": 29}]
    assert allocate(candidates, 14) == []


def test_empty_eligibility_and_capacity_are_safe():
    assert allocate([topic("dependent", 1, prerequisites=("root",))], 60) == []
    assert allocate([topic("a", 0)], 0) == []
