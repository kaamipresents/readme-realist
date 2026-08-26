"""Application factory and process lifecycle.

Run locally with:

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.routes import health, webhooks
from app.services.github.auth import GitHubAppAuth
from app.services.github.client import GitHubClient
from app.services.github.feedback import FeedbackOrchestrator
from app.services.llm.factory import build_backend
from app.services.orchestrator import ReviewPipeline
from app.worker import BackgroundWorker

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Accepts injected settings so tests can override."""
    resolved = settings or get_settings()

    configure_logging(
        level=resolved.log_level,
        json_output=resolved.log_format.value == "json",
        secrets=resolved.secret_values(),
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(resolved.github_timeout_seconds),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            follow_redirects=True,
        )
        backend = build_backend(resolved)

        auth = GitHubAppAuth(
            app_id=resolved.app_id,
            private_key_pem=resolved.private_key_pem,
            http=http,
            api_base_url=resolved.github_api_base_url,
        )
        github = GitHubClient(
            http=http,
            auth=auth,
            api_base_url=resolved.github_api_base_url,
            max_retries=resolved.github_max_retries,
        )
        feedback = FeedbackOrchestrator(
            github,
            drift_conclusion=resolved.drift_check_conclusion,
            post_comment=resolved.post_pr_comment,
            publish_check_run=resolved.publish_check_run,
        )

        application.state.settings = resolved
        application.state.http = http
        application.state.github = github
        application.state.backend = backend
        application.state.pipeline = ReviewPipeline(
            github=github,
            evaluator=backend.evaluator,
            feedback=feedback,
            settings=resolved,
        )
        application.state.worker = BackgroundWorker(max_concurrency=resolved.max_concurrent_reviews)

        logger.info(
            "ReadMe Realist started",
            extra={
                "version": __version__,
                "provider": backend.provider.value,
                "model": backend.model,
                "drift_conclusion": resolved.drift_check_conclusion,
                "docs_globs": resolved.docs_globs,
            },
        )

        try:
            yield
        finally:
            await application.state.worker.drain()
            await http.aclose()
            await backend.aclose()
            logger.info("ReadMe Realist stopped")

    application = FastAPI(
        title="ReadMe Realist",
        description="An automated gatekeeper that stops documentation drift in CI.",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(health.router)
    application.include_router(webhooks.router)
    # Available before startup so `create_app(...)` is useful in tests that
    # never enter the lifespan.
    application.state.settings = resolved
    return application


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazily construct the module-level `app` for `uvicorn app.main:app`.

    Building it eagerly at import time would force every importer — tests
    included — to have a fully populated environment, so the ASGI target is
    resolved on first attribute access instead (PEP 562).
    """
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _app
    if _app is None:
        _app = create_app()
    return _app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_config=None,
    )
