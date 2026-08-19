"""The webhook endpoint, exercised through the real ASGI stack."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.domain import OutcomeKind, PullRequestContext, ReviewOutcome
from app.security.signatures import compute_signature

ENDPOINT = "/webhooks/github"


class RecordingPipeline:
    """Stands in for ReviewPipeline; records what the route handed it."""

    def __init__(self) -> None:
        self.reviewed: list[PullRequestContext] = []
        self.completed = asyncio.Event()

    async def review(self, ctx: PullRequestContext) -> ReviewOutcome:
        self.reviewed.append(ctx)
        self.completed.set()
        return ReviewOutcome(kind=OutcomeKind.EVALUATED, context=ctx, summary="ok")


@pytest.fixture
def pipeline() -> RecordingPipeline:
    return RecordingPipeline()


@pytest.fixture
def client(settings: Settings, pipeline: RecordingPipeline) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        # Swap the pipeline after startup; the route resolves it per request.
        test_client.app.state.pipeline = pipeline  # type: ignore[attr-defined]
        yield test_client


def _post(
    client: TestClient,
    payload: dict[str, Any],
    *,
    secret: str,
    event: str = "pull_request",
    signature: str | None = None,
) -> Any:
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "delivery-abc",
        "X-Hub-Signature-256": signature or compute_signature(body, secret),
        "Content-Type": "application/json",
    }
    return client.post(ENDPOINT, content=body, headers=headers)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_queue_depth(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["pending_reviews"] == 0


# --------------------------------------------------------------------------- #
# Signature enforcement
# --------------------------------------------------------------------------- #


def test_a_valid_delivery_is_accepted_and_queued(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
    pipeline: RecordingPipeline,
) -> None:
    response = _post(client, webhook_payload, secret=webhook_secret)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["repository"] == "acme/widget"
    assert body["pull_number"] == 42

    assert pipeline.reviewed, "the review was never queued"
    assert pipeline.reviewed[0].delivery_id == "delivery-abc"


def test_an_unsigned_delivery_is_rejected(
    client: TestClient, webhook_payload: dict[str, Any], pipeline: RecordingPipeline
) -> None:
    body = json.dumps(webhook_payload).encode()
    response = client.post(
        ENDPOINT,
        content=body,
        headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert pipeline.reviewed == []


def test_a_forged_signature_is_rejected(
    client: TestClient, webhook_payload: dict[str, Any], pipeline: RecordingPipeline
) -> None:
    response = _post(client, webhook_payload, secret="wrong-secret-entirely-different-value")
    assert response.status_code == 401
    assert pipeline.reviewed == []


def test_a_tampered_body_is_rejected(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
    pipeline: RecordingPipeline,
) -> None:
    """Signature computed over the original body; a modified one must fail."""
    original = json.dumps(webhook_payload).encode()
    signature = compute_signature(original, webhook_secret)

    tampered = dict(webhook_payload)
    tampered["action"] = "synchronize"

    response = client.post(
        ENDPOINT,
        content=json.dumps(tampered).encode(),
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert pipeline.reviewed == []


# --------------------------------------------------------------------------- #
# Event routing
# --------------------------------------------------------------------------- #


def test_ping_is_answered(
    client: TestClient, webhook_payload: dict[str, Any], webhook_secret: str
) -> None:
    response = _post(client, {"zen": "hello"}, secret=webhook_secret, event="ping")
    assert response.status_code == 200
    assert response.json()["status"] == "pong"


def test_ping_still_requires_a_valid_signature(client: TestClient, webhook_secret: str) -> None:
    response = _post(client, {"zen": "hello"}, secret="not-the-real-secret-value", event="ping")
    assert response.status_code == 401


@pytest.mark.parametrize("event", ["push", "issues", "check_run", "release"])
def test_unsubscribed_events_are_ignored(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
    pipeline: RecordingPipeline,
    event: str,
) -> None:
    response = _post(client, webhook_payload, secret=webhook_secret, event=event)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert pipeline.reviewed == []


def test_a_closed_pull_request_is_ignored(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
    pipeline: RecordingPipeline,
) -> None:
    payload = dict(webhook_payload)
    payload["action"] = "closed"

    response = _post(client, payload, secret=webhook_secret)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert pipeline.reviewed == []


def test_synchronize_is_queued(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
    pipeline: RecordingPipeline,
) -> None:
    payload = json.loads(json.dumps(webhook_payload))
    payload["action"] = "synchronize"

    response = _post(client, payload, secret=webhook_secret)
    assert response.status_code == 202
    assert pipeline.reviewed[0].action == "synchronize"


def test_a_draft_is_ignored(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
    pipeline: RecordingPipeline,
) -> None:
    payload = json.loads(json.dumps(webhook_payload))
    payload["pull_request"]["draft"] = True

    response = _post(client, payload, secret=webhook_secret)
    assert response.status_code == 200
    assert "draft" in response.json()["reason"]
    assert pipeline.reviewed == []


# --------------------------------------------------------------------------- #
# Malformed input
# --------------------------------------------------------------------------- #


def test_malformed_json_is_a_400(client: TestClient, webhook_secret: str) -> None:
    body = b"{not json"
    response = client.post(
        ENDPOINT,
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": compute_signature(body, webhook_secret),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400
    assert "malformed JSON" in response.json()["reason"]


def test_a_payload_without_an_installation_is_a_400(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
    pipeline: RecordingPipeline,
) -> None:
    payload = json.loads(json.dumps(webhook_payload))
    del payload["installation"]

    response = _post(client, payload, secret=webhook_secret)
    assert response.status_code == 400
    assert "installation.id" in response.json()["reason"]
    assert pipeline.reviewed == []


# --------------------------------------------------------------------------- #
# Asynchrony
# --------------------------------------------------------------------------- #


def test_the_response_does_not_wait_for_a_slow_review(
    client: TestClient,
    webhook_payload: dict[str, Any],
    webhook_secret: str,
) -> None:
    """GitHub times a delivery out in ten seconds; reviews take far longer."""
    review_duration = 1.0
    finished = asyncio.Event()

    class SlowPipeline:
        async def review(self, ctx: PullRequestContext) -> ReviewOutcome:
            await asyncio.sleep(review_duration)
            finished.set()
            return ReviewOutcome(kind=OutcomeKind.EVALUATED, context=ctx, summary="ok")

    client.app.state.pipeline = SlowPipeline()  # type: ignore[attr-defined]

    started_at = time.perf_counter()
    response = _post(client, webhook_payload, secret=webhook_secret)
    elapsed = time.perf_counter() - started_at

    assert response.status_code == 202
    assert elapsed < review_duration / 2, (
        f"the route blocked for {elapsed:.2f}s waiting on the review"
    )
    assert not finished.is_set(), "the review finished before the response returned"
