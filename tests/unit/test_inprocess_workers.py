import asyncio

import pytest

from backend.config.settings import Settings
from backend.orchestration.inprocess_workers import BackgroundWorkers, run_forever


async def _until(predicate, seconds: float = 2.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_a_loop_calls_its_step_again_and_again_until_stopped():
    calls = []

    async def step():
        calls.append(1)

    stop = asyncio.Event()
    task = asyncio.create_task(run_forever("t", step, 0.01, stop))
    await _until(lambda: len(calls) >= 3)
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_a_failing_step_is_retried_and_never_ends_the_loop():
    calls = []

    async def step():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("PayPal was unreachable")

    stop = asyncio.Event()
    task = asyncio.create_task(run_forever("t", step, 0.01, stop))
    await _until(lambda: len(calls) >= 3)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_the_runner_starts_every_step_and_stops_them_all():
    counts = {"recovery": 0, "webhooks": 0}

    def make(name):
        async def step():
            counts[name] += 1

        return step

    workers = BackgroundWorkers(0.01, steps={n: make(n) for n in counts})
    workers.start()
    await _until(lambda: all(c >= 2 for c in counts.values()))
    await workers.stop()
    frozen = dict(counts)
    await asyncio.sleep(0.05)
    assert counts == frozen  # nothing runs after stop()


async def test_stopping_cancels_a_step_that_is_in_the_middle_of_a_slow_call():
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(60)

    workers = BackgroundWorkers(0.01, steps={"slow": slow})
    workers.start()
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(workers.stop(), 1)


def test_the_loops_are_off_by_default_and_the_interval_is_checked():
    assert Settings(_env_file=None).RUN_BACKGROUND_WORKERS is False
    for bad in (0, 0.5, 301, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            Settings(_env_file=None, BACKGROUND_WORKER_INTERVAL_SECONDS=bad)
    assert (
        Settings(_env_file=None, BACKGROUND_WORKER_INTERVAL_SECONDS=5).RUN_BACKGROUND_WORKERS
        is False
    )