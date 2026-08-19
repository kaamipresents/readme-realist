"""Background execution for webhook-triggered reviews.

GitHub expects a webhook response within ten seconds; a full review takes far
longer than that. The route therefore acknowledges with 202 and hands the work
here.

Two behaviours matter beyond "run it later":

* **Bounded concurrency** — a merge queue can deliver a burst of events, and
  without a ceiling every one of them would open a concurrent Opus request.
* **Per-PR deduplication** — pushing three times in quick succession makes the
  first two reviews obsolete before they finish. The newest push wins; earlier
  in-flight reviews for the same pull request are cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

REDIS_URL = os.environ["REDIS_URL"]

logger = logging.getLogger(__name__)

TaskFactory = Callable[[], Awaitable[Any]]


class BackgroundWorker:
    """A small supervised task pool keyed by an arbitrary dedup string."""

    def __init__(self, *, max_concurrency: int = 4) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._closed = False

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def submit(self, key: str, factory: TaskFactory) -> bool:
        """Schedule `factory()`, superseding any in-flight work for `key`.

        Returns False when the worker is shutting down.
        """
        if self._closed:
            logger.warning("worker is shutting down; dropping job", extra={"key": key})
            return False

        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            logger.info("superseding in-flight review", extra={"key": key})
            existing.cancel()

        task = asyncio.create_task(self._run(key, factory), name=f"review:{key}")
        # Hold a reference: asyncio only keeps a weak one, and an unreferenced
        # task can be garbage-collected mid-flight.
        self._tasks[key] = task
        task.add_done_callback(lambda t: self._discard(key, t))
        return True

    async def _run(self, key: str, factory: TaskFactory) -> Any:
        async with self._semaphore:
            try:
                return await factory()
            except asyncio.CancelledError:
                logger.info("review cancelled", extra={"key": key})
                raise
            except Exception:
                logger.exception("background review raised", extra={"key": key})
                return None

    def _discard(self, key: str, task: asyncio.Task[Any]) -> None:
        # Only clear the slot if it still holds *this* task; a newer submission
        # may already have replaced it.
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for in-flight work, then stop accepting more."""
        self._closed = True
        pending = [t for t in self._tasks.values() if not t.done()]
        if not pending:
            return

        logger.info("draining %d in-flight review(s)", len(pending))
        done, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            logger.warning("cancelling review that outlived the drain timeout")
            task.cancel()
        if still_running:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*still_running, return_exceptions=True)
        _ = done
