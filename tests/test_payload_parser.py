"""Webhook payload parsing and trigger subscription."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.models.domain import PullRequestContext
from app.parsers.payload import (
    ACTIONABLE_ACTIONS,
    IgnoredEvent,
    PayloadError,
    parse_pull_request_event,
)


def test_extracts_the_identifiers_the_pipeline_needs(webhook_payload: dict[str, Any]) -> None:
    parsed = parse_pull_request_event(webhook_payload, delivery_id="d-1")

    assert isinstance(parsed, PullRequestContext)
    assert parsed.repo_owner == "acme"
    assert parsed.repo_name == "widget"
    assert parsed.pull_number == 42
    assert parsed.installation_id == 99
    assert parsed.head_sha == "a" * 40
    assert parsed.head_ref == "feature/add-env-var"
    assert parsed.base_ref == "main"
    assert parsed.delivery_id == "d-1"
    assert parsed.slug == "acme/widget#42"


def test_synchronize_is_actionable(webhook_payload: dict[str, Any]) -> None:
    payload = copy.deepcopy(webhook_payload)
    payload["action"] = "synchronize"
    parsed = parse_pull_request_event(payload)
    assert isinstance(parsed, PullRequestContext)
    assert parsed.action == "synchronize"


def test_subscription_is_exactly_opened_and_synchronize() -> None:
    assert frozenset({"opened", "synchronize"}) == ACTIONABLE_ACTIONS


@pytest.mark.parametrize(
    "action", ["closed", "reopened", "edited", "labeled", "ready_for_review", "assigned"]
)
def test_other_actions_are_ignored(webhook_payload: dict[str, Any], action: str) -> None:
    payload = copy.deepcopy(webhook_payload)
    payload["action"] = action
    parsed = parse_pull_request_event(payload)
    assert isinstance(parsed, IgnoredEvent)
    assert action in parsed.reason


def test_drafts_are_ignored_by_default(webhook_payload: dict[str, Any]) -> None:
    payload = copy.deepcopy(webhook_payload)
    payload["pull_request"]["draft"] = True
    parsed = parse_pull_request_event(payload)
    assert isinstance(parsed, IgnoredEvent)
    assert "draft" in parsed.reason


def test_drafts_can_be_opted_into(webhook_payload: dict[str, Any]) -> None:
    payload = copy.deepcopy(webhook_payload)
    payload["pull_request"]["draft"] = True
    parsed = parse_pull_request_event(payload, skip_drafts=False)
    assert isinstance(parsed, PullRequestContext)
    assert parsed.is_draft is True


def test_missing_installation_is_a_hard_error(webhook_payload: dict[str, Any]) -> None:
    payload = copy.deepcopy(webhook_payload)
    del payload["installation"]
    with pytest.raises(PayloadError, match=r"installation\.id"):
        parse_pull_request_event(payload)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("pull_request",), "pull_request"),
        (("repository",), "repository"),
        (("action",), "action"),
    ],
)
def test_missing_required_fields_raise(
    webhook_payload: dict[str, Any], path: tuple[str, ...], expected: str
) -> None:
    payload = copy.deepcopy(webhook_payload)
    del payload[path[0]]
    with pytest.raises(PayloadError, match=expected):
        parse_pull_request_event(payload)


def test_missing_head_sha_raises(webhook_payload: dict[str, Any]) -> None:
    payload = copy.deepcopy(webhook_payload)
    del payload["pull_request"]["head"]["sha"]
    with pytest.raises(PayloadError, match=r"head\.sha"):
        parse_pull_request_event(payload)


def test_log_context_never_leaks_the_full_sha(pr_context: PullRequestContext) -> None:
    context = pr_context.log_context()
    assert context["repo"] == "acme/widget"
    assert context["head_sha"] == "a" * 12
