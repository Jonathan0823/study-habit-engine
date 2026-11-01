import pytest

from app.curriculum import CompletionMapping, curriculum_impact


def topic(topic_id, description, prerequisites=()):
    return {
        "topic_id": topic_id,
        "title": topic_id,
        "description": description,
        "estimate_minutes": 30,
        "position": 0,
        "prerequisites": list(prerequisites),
        "resources": [{"url": "https://example.org/", "title": "resource"}],
    }


def test_split_mapping_preserves_history_identity_and_explicitly_carries_completion():
    old = {"topics": [topic("old", "old objective")]}
    new = {"topics": [topic("part_a", "objective A"), topic("part_b", "objective B")]}
    result = curriculum_impact(
        old,
        new,
        {"old": True},
        [
            CompletionMapping(
                from_topic_ids=["old"], to_topic_id="part_a", carry_completion=True
            )
        ],
    )
    assert result["removed_topic_ids"] == ["old"]
    assert result["new_topic_ids"] == ["part_a", "part_b"]
    assert result["completion_carryover_topic_ids"] == ["part_a"]


def test_changed_objective_with_same_id_needs_mapping_and_never_title_inference():
    old = {"topics": [topic("same", "first objective")]}
    new = {"topics": [topic("same", "different objective")]}
    with pytest.raises(ValueError, match="explicit completion mapping"):
        curriculum_impact(old, new, {"same": True}, [])


def test_mapping_from_retired_source_cannot_target_unchanged_identity():
    old = {
        "topics": [
            topic("retired", "Old objective"),
            topic("retained", "Stable objective"),
        ]
    }
    new = {
        "topics": [
            topic("retained", "Stable objective"),
            topic("fresh", "New objective"),
        ]
    }

    with pytest.raises(ValueError, match="absent from the prior curriculum"):
        curriculum_impact(
            old,
            new,
            {"retired": True, "retained": True},
            [
                CompletionMapping(
                    from_topic_ids=["retired"],
                    to_topic_id="retained",
                    carry_completion=True,
                )
            ],
        )


def test_mapping_cannot_target_reused_id_with_changed_objective():
    old = {
        "topics": [
            topic("retired", "Retired objective"),
            topic("changed", "Prior objective"),
        ]
    }
    new = {"topics": [topic("changed", "Revised objective")]}

    with pytest.raises(ValueError, match="absent from the prior curriculum"):
        curriculum_impact(
            old,
            new,
            {"retired": True, "changed": True},
            [
                CompletionMapping(
                    from_topic_ids=["retired"],
                    to_topic_id="changed",
                    carry_completion=True,
                )
            ],
        )


def test_changed_prerequisites_clear_completed_topic_and_its_completed_dependents():
    old = {
        "topics": [
            topic("a", "A"),
            topic("b", "B", ["a"]),
            topic("d", "D", ["b"]),
            topic("e", "E", ["d"]),
        ]
    }
    new = {
        "topics": [
            topic("a", "A"),
            topic("c", "C"),
            topic("b", "B", ["c"]),
            topic("d", "D", ["b"]),
            topic("e", "E", ["d"]),
        ]
    }

    progress = {topic_id: True for topic_id in ("a", "b", "d", "e")}
    result = curriculum_impact(old, new, progress, [])

    assert result["completion_carryover_topic_ids"] == ["a"]
    assert result["prerequisite_invalidated_topic_ids"] == ["b", "d", "e"]


def test_changed_objective_cannot_reuse_identity_via_mapping():
    old = {"topics": [topic("same", "first objective")]}
    new = {"topics": [topic("same", "different objective")]}
    with pytest.raises(ValueError, match="retired"):
        curriculum_impact(
            old,
            new,
            {"same": True},
            [
                CompletionMapping(
                    from_topic_ids=["same"], to_topic_id="same", carry_completion=True
                )
            ],
        )


def test_carried_completion_is_removed_when_target_prerequisites_are_not_complete():
    old = {"topics": [topic("old", "old objective")]}
    new = {"topics": [topic("prereq", "required"), topic("target", "new", ["prereq"])]}
    result = curriculum_impact(
        old,
        new,
        {"old": True},
        [
            CompletionMapping(
                from_topic_ids=["old"], to_topic_id="target", carry_completion=True
            )
        ],
    )
    assert result["completion_carryover_topic_ids"] == []
    assert "target" in result["prerequisite_invalidated_topic_ids"]
