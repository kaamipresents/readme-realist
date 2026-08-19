"""Webhook payload parsing.

Turns an untrusted `pull_request` webhook body into a validated
`PullRequestContext`, or explains precisely why the event is not actionable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.domain import PullRequestContext

#: The only two triggers ReadMe Realist subscribes to.
ACTIONABLE_ACTIONS: frozenset[str] = frozenset({"opened", "synchronize"})

#: Webhook event names we accept at the route layer.
SUPPORTED_EVENTS: frozenset[str] = frozenset({"pull_request", "ping"})


class PayloadError(ValueError):
    """The payload is structurally unusable (missing required fields)."""


@dataclass(frozen=True, slots=True)
class IgnoredEvent:
    """A well-formed event we deliberately do not act on."""

    reason: str


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping or mapping[key] is None:
        raise PayloadError(f"missing `{where}.{key}` in webhook payload")
    return mapping[key]


def parse_pull_request_event(
    payload: dict[str, Any],
    *,
    delivery_id: str = "",
    skip_drafts: bool = True,
) -> PullRequestContext | IgnoredEvent:
    """Extract `repo_owner`, `repo_name`, `pull_number` and friends.

    Returns an `IgnoredEvent` for actions outside the subscription, and raises
    `PayloadError` when a field we genuinely need is absent.
    """
    action = payload.get("action")
    if not isinstance(action, str):
        raise PayloadError("missing `action` in webhook payload")
    if action not in ACTIONABLE_ACTIONS:
        return IgnoredEvent(
            reason=f"action `{action}` is outside the subscription "
            f"({', '.join(sorted(ACTIONABLE_ACTIONS))})"
        )

    pull_request = _require(payload, "pull_request", "payload")
    repository = _require(payload, "repository", "payload")
    installation = payload.get("installation")

    if not isinstance(installation, dict) or "id" not in installation:
        raise PayloadError(
            "missing `installation.id` — ReadMe Realist authenticates as a GitHub App "
            "and cannot mint a token without it"
        )

    owner = _require(_require(repository, "owner", "repository"), "login", "repository.owner")
    name = _require(repository, "name", "repository")
    number = _require(pull_request, "number", "pull_request")
    head = _require(pull_request, "head", "pull_request")
    base = pull_request.get("base") or {}

    is_draft = bool(pull_request.get("draft", False))
    if is_draft and skip_drafts:
        return IgnoredEvent(reason="pull request is a draft")

    try:
        pull_number = int(number)
        installation_id = int(installation["id"])
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise PayloadError(f"non-integer identifier in payload: {exc}") from exc

    return PullRequestContext(
        repo_owner=str(owner),
        repo_name=str(name),
        pull_number=pull_number,
        head_sha=str(_require(head, "sha", "pull_request.head")),
        head_ref=str(head.get("ref", "")),
        base_ref=str(base.get("ref", "")),
        installation_id=installation_id,
        action=action,
        is_draft=is_draft,
        title=str(pull_request.get("title") or ""),
        html_url=str(pull_request.get("html_url") or ""),
        delivery_id=delivery_id,
    )
