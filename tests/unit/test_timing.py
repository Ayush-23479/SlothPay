from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from backend.api.timing_deps import Timed
from backend.notifications.hub import InMemoryHub, publish_safely
from backend.reliability import timing
from backend.reliability.reconcile import StatusReconciler, StepOutcome


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, ms: float) -> None:
        self.now += ms / 1000


def test_kinds_are_kept_apart_and_overhead_ignores_provider_and_human():
    clock = Clock()
    timer = timing.StepTimer(clock=clock)
    with timer.step("db_write"):
        clock.tick(7)
    timer.add("paypal_create_order", timing.PROVIDER, 400)
    timer.add("buyer_approval", timing.HUMAN, 9000)
    timer.add("reconcile_wait", timing.WAIT, 300)
    summary = timer.summary()
    assert timing.overhead_ms(summary) == pytest.approx(7)


def test_stop_adds_other_overhead_so_steps_add_up_to_the_wall_time():
    clock = Clock()
    timer = timing.StepTimer(clock=clock)
    with timer.step("db_write"):
        clock.tick(10)
    clock.tick(5)  # glue between steps
    timer.add("paypal_create_order", timing.PROVIDER, 0)
    timer.stop()
    timer.stop()  # safe twice
    other = [s for s in timer.steps if s.name == timing.OTHER_OVERHEAD]
    assert len(other) == 1 and other[0].ms == pytest.approx(5)
    assert timer.wall_ms == pytest.approx(15)


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        timing.StepTimer().add("x", "bogus", 1)


def test_percentile_matches_linear_interpolation():
    assert timing.percentile([], 95) is None
    assert timing.percentile([5], 95) == 5
    assert timing.percentile([0, 10], 50) == 5
    assert timing.percentile(range(1, 101), 95) == pytest.approx(95.05)
    with pytest.raises(ValueError):
        timing.percentile([1], 101)


def test_summarize_rows_separates_kinds_per_payment():
    rows = []
    for index in range(20):
        intent = f"p{index}"
        rows += [
            {"intent_id": intent, "step": "db_write", "kind": "ours", "ms": 10 + index},
            {"intent_id": intent, "step": "paypal_create_order", "kind": "provider", "ms": 500},
            {"intent_id": intent, "step": "reconcile_wait", "kind": "wait", "ms": 300},
        ]
    summary = timing.summarize_rows(rows)
    assert summary["payments"] == 20
    assert summary["overhead"]["p50_ms"] == pytest.approx(19.5)
    assert summary["provider"]["p50_ms"] == 500
    assert summary["steps"]["reconcile_wait"]["kind"] == "wait"
    # Deliberate waiting never counts as SlothPay's time.
    assert summary["overhead"]["p95_ms"] < 40


def test_provider_step_names():
    assert timing.provider_step_name("capture_authorization") == "paypal_capture"
    assert timing.provider_step_name("create_order") == "paypal_create_order"


async def test_provider_label_renames_the_read_steps():
    timer = timing.StepTimer()
    token = timing.activate(timer)
    try:
        timing.record_provider("get_order", 12)
        with timing.provider_label(timing.RECONCILE_READ):
            timing.record_provider("get_order", 20)
    finally:
        timing.deactivate(token)
    names = [(s.name, s.kind) for s in timer.steps]
    assert names == [(timing.READ_STEP, "provider"), (timing.RECONCILE_READ, "provider")]


async def test_after_response_is_queued_behind_a_request_and_spawned_without_one():
    ran: list[str] = []

    async def job():
        ran.append("job")

    assert timing.after_response(job) is False  # no request: started right away
    await timing.wait_background()
    assert ran == ["job"]

    timer = timing.StepTimer()
    token = timing.activate(timer)
    try:
        assert timing.after_response(job) is True
    finally:
        timing.deactivate(token)
    await asyncio.sleep(0)
    assert ran == ["job"]  # still waiting for the response
    await timing.run_after_response(timer)
    await timing.wait_background()
    assert ran == ["job", "job"]


async def test_a_failing_after_response_job_does_not_stop_the_next():
    ran: list[str] = []

    async def bad():
        raise RuntimeError("boom")

    async def good():
        ran.append("good")

    timer = timing.StepTimer()
    timer.after += [bad, good]
    await timing.run_after_response(timer)
    await timing.wait_background()
    assert ran == ["good"]


async def test_publish_safely_never_raises_and_is_bounded():
    class Broken:
        async def publish(self, *_a, **_k):
            raise RuntimeError("down")

    class Slow:
        async def publish(self, *_a, **_k):
            await asyncio.sleep(5)

    await publish_safely(Broken(), "p1", {"type": "x"})
    started = asyncio.get_running_loop().time()
    await publish_safely(Slow(), "p1", {"type": "x"}, timeout=0.05)
    assert asyncio.get_running_loop().time() - started < 1
    await publish_safely(InMemoryHub(), "p1", {"type": "x"})


async def test_first_reconcile_check_waits_0_3_seconds_and_the_wait_is_not_overhead():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def step(_intent: str) -> StepOutcome:
        return StepOutcome("EVIDENCE_APPLIED", "CAPTURED")

    timer = timing.StepTimer()
    token = timing.activate(timer)
    try:
        result = await StatusReconciler(step, sleep=fake_sleep).reconcile_detailed("x")
    finally:
        timing.deactivate(token)
    assert slept[0] == 0.3 and result.status == "CAPTURED"
    assert any(s.name == timing.RECONCILE_WAIT and s.kind == "wait" for s in timer.steps)
    assert timing.overhead_ms(timer.summary()) == 0


async def test_timed_dependency_reaches_the_endpoint_and_runs_jobs_after_the_response():
    events: list[str] = []
    app = FastAPI()

    @app.get("/ok")
    async def ok(timer: Timed):
        assert timing.current() is timer
        with timing.step("db_write"):
            pass
        timing.after_response(lambda: events.append("after"))
        events.append("handler")
        return {"ok": True}

    @app.get("/err")
    async def err(timer: Timed):
        timing.after_response(lambda: events.append("after-error"))
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        assert (await http.get("/ok")).status_code == 200
        assert (await http.get("/err")).status_code == 500
    await timing.wait_background()
    assert events == ["handler", "after", "after-error"]


def test_deliberate_waits_never_become_other_overhead():
    clock = Clock()
    timer = timing.StepTimer(clock=clock)
    clock.tick(300)
    timer.add("reconcile_wait", timing.WAIT, 300)
    timer.stop()
    assert timing.overhead_ms(timer.summary()) == 0