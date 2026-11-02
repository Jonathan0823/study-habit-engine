"""Structural validation for AI-proposed learning plans."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
QUALITY_CRITERIA = (
    "goal_coverage",
    "objective_assessment_alignment",
    "topic_sequence",
    "practice_and_retrieval",
    "scope_and_workload",
)
QUALITY_CRITERIA_GUIDANCE = {
    "goal_coverage": "Outcomes and topics address the learner's goal without unnecessary breadth.",
    "objective_assessment_alignment": "Each observable outcome has an assessment that would reveal it.",
    "topic_sequence": "Topics build from prerequisite foundations toward application or synthesis.",
    "practice_and_retrieval": "Activities require active recall or application, not only consuming materials.",
    "scope_and_workload": "Topic estimates fit the learner's weekly budget and proposed duration.",
}
QUALITY_REFERENCES = (
    {
        "title": "Carnegie Mellon Eberly Center: Course Content & Schedule",
        "url": "https://www.cmu.edu/teaching/designteach/design/contentschedule.html",
    },
    {
        "title": "Carnegie Mellon Eberly Center: Align Assessments, Objectives, Instructional Strategies",
        "url": "https://www.cmu.edu/teaching/assessment/basics/alignment.html",
    },
    {
        "title": "The Learning Scientists: Six Strategies for Effective Learning",
        "url": "https://www.learningscientists.org/blog/2019/11/28-1",
    },
)


def validate_learning_plan(
    plan: object,
    *,
    trusted_hosts: set[str],
    weekly_minutes: int,
    candidate_urls: set[str] | None = None,
) -> list[str]:
    """Return deterministic structural/policy errors; an empty list means valid.

    This checks the plan's shape and configured safety constraints, not whether
    the proposed learning experience is educationally effective.
    """
    errors: list[str] = []
    if not isinstance(plan, dict) or set(plan) != {
        "goal_summary",
        "duration_weeks",
        "outcomes",
        "topics",
        "quality_review",
    }:
        return ["INVALID_PLAN_SHAPE"]
    if not _nonempty(plan.get("goal_summary"), 2000):
        errors.append("INVALID_GOAL_SUMMARY")
    duration = plan.get("duration_weeks")
    if type(duration) is not int or not 1 <= duration <= 104:
        errors.append("INVALID_DURATION")
    if type(weekly_minutes) is not int or not 1 <= weekly_minutes <= 10080:
        errors.append("INVALID_WEEKLY_BUDGET")

    outcomes = plan.get("outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        errors.append("MISSING_OUTCOMES")
        outcomes = []
    outcome_ids: set[str] = set()
    for outcome in outcomes:
        if not isinstance(outcome, dict) or set(outcome) != {
            "outcome_id",
            "description",
            "assessment",
        }:
            errors.append("INVALID_OUTCOME")
            continue
        outcome_id = outcome.get("outcome_id")
        if not _valid_id(outcome_id) or outcome_id in outcome_ids:
            errors.append("INVALID_OUTCOME_ID")
        else:
            outcome_ids.add(outcome_id)
        if not _nonempty(outcome.get("description"), 1000):
            errors.append("INVALID_OUTCOME_DESCRIPTION")
        if not _nonempty(outcome.get("assessment"), 1000):
            errors.append("INVALID_OUTCOME_ASSESSMENT")

    topics = plan.get("topics")
    if not isinstance(topics, list) or not topics:
        errors.append("MISSING_TOPICS")
        topics = []
    topic_by_id: dict[str, dict] = {}
    for topic in topics:
        if not isinstance(topic, dict) or set(topic) != {
            "topic_id",
            "title",
            "description",
            "outcome_ids",
            "prerequisites",
            "estimate_minutes",
            "practice",
            "resources",
        }:
            errors.append("INVALID_TOPIC")
            continue
        topic_id = topic.get("topic_id")
        if not _valid_id(topic_id) or topic_id in topic_by_id:
            errors.append("INVALID_TOPIC_ID")
            continue
        topic_by_id[topic_id] = topic
        for key, code in (("title", "INVALID_TOPIC_TITLE"), ("description", "INVALID_TOPIC_DESCRIPTION"), ("practice", "INVALID_TOPIC_PRACTICE")):
            if not _nonempty(topic.get(key), 2000):
                errors.append(code)
        estimate = topic.get("estimate_minutes")
        if type(estimate) is not int or not 1 <= estimate <= 10080:
            errors.append("INVALID_TOPIC_ESTIMATE")
        references = topic.get("outcome_ids")
        if not isinstance(references, list) or not references:
            errors.append("MISSING_TOPIC_OUTCOMES")
        elif any(not isinstance(ref, str) or ref not in outcome_ids for ref in references):
            errors.append("UNKNOWN_TOPIC_OUTCOME")
        prerequisites = topic.get("prerequisites")
        if not isinstance(prerequisites, list) or any(not isinstance(ref, str) for ref in prerequisites):
            errors.append("INVALID_PREREQUISITES")
        resources = topic.get("resources")
        if not isinstance(resources, list) or not resources:
            errors.append("MISSING_TOPIC_RESOURCES")
            continue
        for resource in resources:
            errors.extend(_resource_errors(resource, trusted_hosts, candidate_urls))

    linked_outcomes = {
        outcome_id
        for topic in topic_by_id.values()
        for outcome_id in topic.get("outcome_ids", [])
        if isinstance(outcome_id, str)
    }
    if outcome_ids - linked_outcomes:
        errors.append("UNLINKED_OUTCOME")

    topic_order = {topic_id: position for position, topic_id in enumerate(topic_by_id)}
    dependency_graph: dict[str, list[str]] = {}
    for topic_id, topic in topic_by_id.items():
        prerequisites = topic.get("prerequisites")
        if not isinstance(prerequisites, list) or any(
            not isinstance(prerequisite, str) for prerequisite in prerequisites
        ):
            continue
        dependency_graph[topic_id] = []
        for prerequisite in prerequisites:
            if prerequisite not in topic_by_id or prerequisite == topic_id:
                errors.append("UNKNOWN_OR_SELF_PREREQUISITE")
                continue
            dependency_graph[topic_id].append(prerequisite)
            if topic_order[prerequisite] >= topic_order[topic_id]:
                errors.append("INVALID_TOPIC_ORDER")
    if _has_cycle(dependency_graph):
        errors.append("PREREQUISITE_CYCLE")

    _validate_quality_review(
        plan.get("quality_review"),
        topic_ids=set(topic_by_id),
        outcome_ids=outcome_ids,
        errors=errors,
    )

    if (
        type(duration) is int
        and 1 <= duration <= 104
        and type(weekly_minutes) is int
        and 1 <= weekly_minutes <= 10080
        and all(type(topic.get("estimate_minutes")) is int for topic in topic_by_id.values())
        and sum(topic["estimate_minutes"] for topic in topic_by_id.values())
        > duration * weekly_minutes
    ):
        errors.append("ESTIMATES_EXCEED_AVAILABLE_TIME")
    return errors


def _validate_quality_review(
    review: object,
    *,
    topic_ids: set[str],
    outcome_ids: set[str],
    errors: list[str],
) -> None:
    if not isinstance(review, list) or len(review) != len(QUALITY_CRITERIA):
        errors.append("INVALID_QUALITY_REVIEW")
        return
    by_criterion = {}
    for item in review:
        if not isinstance(item, dict) or set(item) != {
            "criterion",
            "evidence",
            "topic_ids",
            "outcome_ids",
        }:
            errors.append("INVALID_QUALITY_REVIEW_ITEM")
            continue
        criterion = item.get("criterion")
        if criterion not in QUALITY_CRITERIA or criterion in by_criterion:
            errors.append("INVALID_QUALITY_REVIEW_CRITERION")
            continue
        if not _nonempty(item.get("evidence"), 1000):
            errors.append("INVALID_QUALITY_REVIEW_EVIDENCE")
        related_topics = item.get("topic_ids")
        related_outcomes = item.get("outcome_ids")
        valid_topics = isinstance(related_topics, list) and all(
            isinstance(value, str) for value in related_topics
        )
        if (
            not valid_topics
            or not related_topics
            or len(related_topics) != len(set(related_topics))
            or not set(related_topics) <= topic_ids
        ):
            errors.append("INVALID_QUALITY_REVIEW_TOPICS")
        valid_outcomes = isinstance(related_outcomes, list) and all(
            isinstance(value, str) for value in related_outcomes
        )
        if (
            not valid_outcomes
            or len(related_outcomes) != len(set(related_outcomes))
            or not set(related_outcomes) <= outcome_ids
        ):
            errors.append("INVALID_QUALITY_REVIEW_OUTCOMES")
        by_criterion[criterion] = {
            "topic_ids": related_topics if valid_topics else [],
            "outcome_ids": related_outcomes if valid_outcomes else [],
        }
    if set(by_criterion) != set(QUALITY_CRITERIA):
        errors.append("MISSING_QUALITY_REVIEW_CRITERIA")
        return
    for criterion, item in by_criterion.items():
        if criterion in ("goal_coverage", "objective_assessment_alignment"):
            if set(item["outcome_ids"]) != outcome_ids:
                errors.append("INCOMPLETE_QUALITY_REVIEW_OUTCOMES")
        if set(item["topic_ids"]) != topic_ids:
            errors.append("INCOMPLETE_QUALITY_REVIEW_TOPICS")


def _resource_errors(
    resource: object,
    trusted_hosts: set[str],
    candidate_urls: set[str] | None,
) -> list[str]:
    required_fields = {"title", "publisher", "url", "rationale"}
    optional_fields = {"published_at", "checked_at"}
    if (
        not isinstance(resource, dict)
        or not required_fields <= set(resource)
        or not set(resource) <= required_fields | optional_fields
    ):
        return ["INVALID_RESOURCE"]
    if not _nonempty(resource.get("title"), 500):
        return ["INVALID_RESOURCE_TITLE"]
    if not _nonempty(resource.get("publisher"), 500):
        return ["INVALID_RESOURCE_PUBLISHER"]
    if not _nonempty(resource.get("rationale"), 1000):
        return ["INVALID_RESOURCE_RATIONALE"]
    published_at = resource.get("published_at")
    if published_at is not None and not _nonempty(published_at, 200):
        return ["INVALID_RESOURCE_DATE"]
    checked_at = resource.get("checked_at")
    if checked_at is not None and not _nonempty(checked_at, 50):
        return ["INVALID_RESOURCE_CHECKED_AT"]
    url = resource.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        return ["UNTRUSTED_RESOURCE_URL"]
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return ["UNTRUSTED_RESOURCE_URL"]
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.netloc
    ):
        return ["UNTRUSTED_RESOURCE_URL"]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ["UNTRUSTED_RESOURCE_URL"]
    allowed_hosts = {
        item.casefold().rstrip(".") for item in trusted_hosts if isinstance(item, str)
    }
    if host not in allowed_hosts:
        return ["UNTRUSTED_RESOURCE_HOST"]
    if candidate_urls is not None and url not in candidate_urls:
        return ["RESOURCE_NOT_FROM_SEARCH_RESULTS"]
    return []


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(topic_id: str) -> bool:
        if topic_id in visiting:
            return True
        if topic_id in visited:
            return False
        visiting.add(topic_id)
        if any(visit(prerequisite) for prerequisite in graph.get(topic_id, [])):
            return True
        visiting.remove(topic_id)
        visited.add(topic_id)
        return False

    return any(visit(topic_id) for topic_id in graph)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _nonempty(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
