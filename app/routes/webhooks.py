"""GitHub webhook receiver.

Verifies the signature against the raw body, decides whether the event is
actionable, and hands the work to the background worker so GitHub gets its
acknowledgement inside the delivery timeout.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, Request, Response, status

from app.parsers.payload import (
    IgnoredEvent,
    PayloadError,
    parse_pull_request_event,
)
from app.security.signatures import verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    response: Response,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict[str, Any]:
    """Entry point for `pull_request` deliveries."""
    ctx_state = request.app.state
    settings = ctx_state.settings
    delivery_id = x_github_delivery or ""

    # Read the raw bytes — the MAC is over exactly these, so the body must not
    # be re-serialised before verification.
    raw_body = await request.body()

    if not verify_signature(raw_body, x_hub_signature_256, settings.webhook_secret):
        logger.warning(
            "rejected webhook with invalid signature",
            extra={"delivery_id": delivery_id, "event": x_github_event},
        )
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"status": "rejected", "reason": "invalid signature"}

    event = (x_github_event or "").strip()

    if event == "ping":
        # GitHub's install-time handshake. 202 would be misleading — nothing
        # was queued.
        response.status_code = status.HTTP_200_OK
        return {"status": "pong"}

    if event != "pull_request":
        response.status_code = status.HTTP_200_OK
        return {"status": "ignored", "reason": f"unsupported event `{event}`"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"status": "rejected", "reason": f"malformed JSON: {exc}"}

    try:
        parsed = parse_pull_request_event(
            payload,
            delivery_id=delivery_id,
            skip_drafts=settings.skip_draft_pull_requests,
        )
    except PayloadError as exc:
        logger.warning(
            "unusable pull_request payload",
            extra={"delivery_id": delivery_id, "error": str(exc)},
        )
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"status": "rejected", "reason": str(exc)}

    if isinstance(parsed, IgnoredEvent):
        response.status_code = status.HTTP_200_OK
        return {"status": "ignored", "reason": parsed.reason}

    pipeline = ctx_state.pipeline
    worker = ctx_state.worker

    accepted = worker.submit(parsed.slug, lambda: pipeline.review(parsed))
    if not accepted:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "rejected", "reason": "server is shutting down"}

    logger.info("queued review", extra=parsed.log_context())
    return {
        "status": "accepted",
        "repository": parsed.full_name,
        "pull_number": parsed.pull_number,
        "action": parsed.action,
    }
