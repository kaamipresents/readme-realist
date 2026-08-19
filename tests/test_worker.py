"""Background worker: concurrency ceiling, per-PR deduplication, drain."""

from __future__ import annotations

import asyncio

from app.worker import BackgroundWorker


async def test_runs_a_submitted_job() -> None:
    worker = BackgroundWorker()
    ran = asyncio.Event()

    async def job() -> None:
        ran.set()

    assert worker.submit("acme/widget#1", job) is True
    await asyncio.wait_for(ran.wait(), timeout=1)
    await worker.drain()


async def test_a_new_push_supersedes_the_in_flight_review() -> None:
    """The first review is about a head_sha nobody is looking at any more."""
    worker = BackgroundWorker()
    started = asyncio.Event()
    first_cancelled = asyncio.Event()
    second_ran = asyncio.Event()

    async def slow_first() -> None:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            first_cancelled.set()
            raise

    async def fast_second() -> None:
        second_ran.set()

    worker.submit("acme/widget#42", slow_first)
    await asyncio.wait_for(started.wait(), timeout=1)

    worker.submit("acme/widget#42", fast_second)
    await asyncio.wait_for(first_cancelled.wait(), timeout=1)
    await asyncio.wait_for(second_ran.wait(), timeout=1)

    await worker.drain()


async def test_different_pull_requests_run_independently() -> None:
    worker = BackgroundWorker(max_concurrency=4)
    done: list[str] = []

    def make(key: str):
        async def job() -> None:
            done.append(key)

        return job

    for key in ["a#1", "b#2", "c#3"]:
        worker.submit(key, make(key))

    await worker.drain()
    assert sorted(done) == ["a#1", "b#2", "c#3"]


async def test_concurrency_is_bounded() -> None:
    """A merge-queue burst must not open one Opus request per event."""
    worker = BackgroundWorker(max_concurrency=2)
    running = 0
    peak = 0
    release = asyncio.Event()

    def make():
        async def job() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1

        return job

    for index in range(6):
        worker.submit(f"pr#{index}", make())

    await asyncio.sleep(0.05)
    assert peak <= 2

    release.set()
    await worker.drain()
    assert peak <= 2


async def test_a_raising_job_does_not_kill_the_worker() -> None:
    worker = BackgroundWorker()
    survived = asyncio.Event()

    async def explodes() -> None:
        raise RuntimeError("boom")

    async def follows() -> None:
        survived.set()

    worker.submit("pr#1", explodes)
    await asyncio.sleep(0.01)
    worker.submit("pr#2", follows)

    await asyncio.wait_for(survived.wait(), timeout=1)
    await worker.drain()


async def test_completed_jobs_are_evicted_from_the_registry() -> None:
    worker = BackgroundWorker()

    async def job() -> None:
        return None

    worker.submit("pr#1", job)
    await worker.drain()
    assert worker.pending == 0


async def test_drain_waits_for_in_flight_work() -> None:
    worker = BackgroundWorker()
    finished = asyncio.Event()

    async def job() -> None:
        await asyncio.sleep(0.05)
        finished.set()

    worker.submit("pr#1", job)
    await worker.drain(timeout=5)
    assert finished.is_set()


async def test_drain_cancels_work_that_outlives_the_timeout() -> None:
    worker = BackgroundWorker()
    started = asyncio.Event()

    async def hangs() -> None:
        started.set()
        await asyncio.sleep(30)

    worker.submit("pr#1", hangs)
    await asyncio.wait_for(started.wait(), timeout=1)

    await worker.drain(timeout=0.05)
    assert worker.pending == 0


async def test_submissions_are_refused_after_drain() -> None:
    worker = BackgroundWorker()
    await worker.drain()

    async def job() -> None:
        return None

    assert worker.submit("pr#1", job) is False
