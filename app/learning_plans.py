"""Structural validation for AI-proposed learning plans."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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
