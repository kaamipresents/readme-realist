"""Liveness and readiness probes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, Any]:
    """Readiness: dependencies are wired and the queue is not wedged."""
    worker = getattr(request.app.state, "worker", None)
    return {
        "status": "ready",
        "version": __version__,
        "pending_reviews": worker.pending if worker is not None else 0,
    }
