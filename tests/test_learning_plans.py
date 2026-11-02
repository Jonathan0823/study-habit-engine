from app.learning_plans import validate_learning_plan


def valid_plan():
    return {
        "goal_summary": "Build a small Python command-line application.",
        "duration_weeks": 4,
        "outcomes": [
            {
                "outcome_id": "write-python",
                "description": "Write a small Python program using functions and collections.",
                "assessment": "Implement and explain a command-line script.",
            }
        ],
        "topics": [
            {
                "topic_id": "python-basics",
                "title": "Python basics",
                "description": "Practice expressions, control flow, and functions.",
                "outcome_ids": ["write-python"],
                "prerequisites": [],
                "estimate_minutes": 180,
                "practice": "Use retrieval questions, then write a small function without notes.",
                "resources": [
                    {
                        "title": "The Python Tutorial",
                        "publisher": "Python Software Foundation",
                        "url": "https://docs.python.org/3/tutorial/",
                        "rationale": "Covers the language concepts used in this topic.",
                    }
                ],
            }
        ],
        "quality_review": [
            {
                "criterion": "goal_coverage",
                "evidence": "The Python outcome is covered by the foundational topic.",
                "topic_ids": ["python-basics"],
                "outcome_ids": ["write-python"],
            },
            {
                "criterion": "objective_assessment_alignment",
                "evidence": "The implementation assessment measures the stated outcome.",
                "topic_ids": ["python-basics"],
                "outcome_ids": ["write-python"],
            },
            {
                "criterion": "topic_sequence",
                "evidence": "The foundational topic has no unmet prerequisites.",
                "topic_ids": ["python-basics"],
                "outcome_ids": [],
            },
            {
                "criterion": "practice_and_retrieval",
                "evidence": "The topic uses retrieval before independent coding practice.",
                "topic_ids": ["python-basics"],
                "outcome_ids": [],
            },
            {
                "criterion": "scope_and_workload",
                "evidence": "The estimated effort fits within the learner's budget.",
                "topic_ids": ["python-basics"],
                "outcome_ids": [],
            },
        ],
    }


def test_valid_learning_plan_has_consistent_outcomes_prerequisites_and_trusted_resources():
    assert validate_learning_plan(
        valid_plan(),
        trusted_hosts={"docs.python.org"},
        weekly_minutes=300,
    ) == []


def test_malformed_prerequisite_values_are_rejected_without_crashing():
    plan = valid_plan()
    plan["topics"][0]["prerequisites"] = [{"topic": "not-an-id"}]

    assert "INVALID_PREREQUISITES" in validate_learning_plan(
        plan,
        trusted_hosts={"docs.python.org"},
        weekly_minutes=300,
    )


def test_quality_review_must_cover_each_criterion_and_valid_plan_ids():
    plan = valid_plan()
    plan["quality_review"] = plan["quality_review"][:-1]
    errors = validate_learning_plan(
        plan,
        trusted_hosts={"docs.python.org"},
        weekly_minutes=300,
    )
    assert "INVALID_QUALITY_REVIEW" in errors

    plan = valid_plan()
    plan["quality_review"][0]["topic_ids"] = [{"unhashable": "value"}]
    errors = validate_learning_plan(
        plan,
        trusted_hosts={"docs.python.org"},
        weekly_minutes=300,
    )
    assert "INVALID_QUALITY_REVIEW_TOPICS" in errors


def test_resource_host_must_match_allowlist_exactly():
    plan = valid_plan()
    plan["topics"][0]["resources"][0]["url"] = (
        "https://docs.python.org.attacker.example/tutorial/"
    )

    assert "UNTRUSTED_RESOURCE_HOST" in validate_learning_plan(
        plan,
        trusted_hosts={"docs.python.org"},
        weekly_minutes=300,
    )
