from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("slothpay.timing")

OURS = "ours"
PROVIDER = "provider"
HUMAN = "human"
WAIT = "wait"
KINDS = (OURS, PROVIDER, HUMAN, WAIT)
COUNTED_KINDS = (OURS, PROVIDER, HUMAN)

LABEL = "sandbox"

# PayPal operation (as named by the client) -> the step name shown to people.
_PROVIDER_STEPS = {
    "create_order": "paypal_create_order",
    "authorize_order": "paypal_authorize",
    "capture_order": "paypal_capture",
    "capture_authorization": "paypal_capture",
    "void_authorization": "paypal_void",
    "oauth_token": "paypal_token",
}
READ_STEP = "paypal_read"
RECONCILE_READ = "reconcile_read"
RECONCILE_WAIT = "reconcile_wait"
CONDITION_TO_CAPTURE = "condition_to_capture"
BUYER_APPROVAL = "buyer_approval"
OTHER_OVERHEAD = "other_overhead"


@dataclass(frozen=True)
class Step:
    name: str
    kind: str
    ms: float


@dataclass
class StepTimer:
    """The steps of one request (or one background job) for one payment."""

    intent_id: str | None = None
    clock: Callable[[], float] = time.perf_counter
    steps: list[Step] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)
    after: list[Callable[[], Any]] = field(default_factory=list)
    started: float = 0.0
    wall_ms: float | None = None

    def __post_init__(self) -> None:
        self.started = self.clock()

    # ------------------------------------------------------------------ recording
    def add(self, name: str, kind: str, ms: float) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown step kind: {kind!r}")
        value = float(ms)
        if not math.isfinite(value):
            return
        self.steps.append(Step(name, kind, round(max(0.0, value), 3)))

    @contextmanager
    def step(self, name: str, kind: str = OURS) -> Iterator[None]:
        """Time the block. Do not wrap a PayPal call in an `ours` step: it would count twice."""
        if kind not in KINDS:
            raise ValueError(f"unknown step kind: {kind!r}")
        began = self.clock()
        try:
            yield
        finally:
            self.add(name, kind, (self.clock() - began) * 1000)

    def mark(self, name: str) -> None:
        self.marks[name] = self.clock()

    def since(self, name: str) -> float | None:
        began = self.marks.get(name)
        return None if began is None else (self.clock() - began) * 1000

    def stop(self) -> None:
        """The handler is done. Whatever time was not measured by a step is SlothPay's own
        (parsing, serialising, glue), so it is added as `other_overhead` and the steps then add
        up to the real elapsed time. Safe to call twice."""
        if self.wall_ms is not None:
            return
        self.wall_ms = (self.clock() - self.started) * 1000
        # Deliberate waits are measured too, so they never turn into `other_overhead`.
        measured = sum(s.ms for s in self.steps if s.kind in (OURS, PROVIDER, WAIT))
        rest = self.wall_ms - measured
        if rest > 0:
            self.add(OTHER_OVERHEAD, OURS, rest)

    # -------------------------------------------------------------------- reading
    def summary(self) -> dict[str, Any]:
        return summarize_steps(self.steps)


def summarize_steps(steps: Iterable[Step]) -> dict[str, Any]:
    items = list(steps)
    totals = {kind: round(sum(s.ms for s in items if s.kind == kind), 3) for kind in KINDS}
    return {
        "total_ms": round(totals[OURS] + totals[PROVIDER] + totals[HUMAN], 3),
        "ours_ms": totals[OURS],
        "provider_ms": totals[PROVIDER],
        "human_ms": totals[HUMAN],
        "steps": [{"name": s.name, "kind": s.kind, "ms": s.ms} for s in items],
    }


def overhead_ms(summary: dict[str, Any]) -> float:
    """SlothPay's own added time: `ours` only. PayPal's time and the buyer's time are not ours."""
    return float(summary["ours_ms"])


# ------------------------------------------------------------------ the current timer
_current: ContextVar[StepTimer | None] = ContextVar("slothpay_step_timer", default=None)
_label: ContextVar[str | None] = ContextVar("slothpay_provider_label", default=None)


def current() -> StepTimer | None:
    return _current.get()


def activate(timer: StepTimer) -> Token:
    return _current.set(timer)


def deactivate(token: Token) -> None:
    try:
        _current.reset(token)
    except ValueError:  # reset from another context: the timer is simply dropped
        _current.set(None)


@contextmanager
def step(name: str, kind: str = OURS) -> Iterator[None]:
    """Time a block on the current timer. A plain no-op when no timer is active."""
    timer = _current.get()
    if timer is None:
        yield
        return
    with timer.step(name, kind):
        yield


def bind_intent(intent_id: str | None) -> None:
    timer = _current.get()
    if timer is not None and intent_id and timer.intent_id is None:
        timer.intent_id = str(intent_id)


def mark(name: str) -> None:
    timer = _current.get()
    if timer is not None:
        timer.mark(name)


def add_step(name: str, kind: str, ms: float) -> None:
    timer = _current.get()
    if timer is not None:
        timer.add(name, kind, ms)


@contextmanager
def provider_label(name: str) -> Iterator[None]:
    """Name the PayPal calls made inside the block (for example `reconcile_read`)."""
    token = _label.set(name)
    try:
        yield
    finally:
        _label.reset(token)


def provider_step_name(operation: str) -> str:
    return _label.get() or _PROVIDER_STEPS.get(operation, READ_STEP)


def record_provider(operation: str, ms: float) -> None:
    """The PayPal client reports how long PayPal took, kept apart from our own time."""
    timer = _current.get()
    if timer is not None:
        timer.add(provider_step_name(operation), PROVIDER, ms)


def note_send(operation: str) -> None:
    """The PayPal client is about to send. For a capture that follows a verified condition this
    closes the `condition_to_capture` measurement (verified -> capture request on the wire)."""
    timer = _current.get()
    if timer is None or operation != "capture_authorization":
        return
    elapsed = timer.since("condition_verified")
    if elapsed is not None and not any(s.name == CONDITION_TO_CAPTURE for s in timer.steps):
        timer.add(CONDITION_TO_CAPTURE, OURS, elapsed)


# ------------------------------------------------------------------ after the response
_background: set[asyncio.Task] = set()


def spawn(coro: Awaitable[Any], name: str = "slothpay-background") -> asyncio.Task:
    """Run a coroutine in the background. Its failure is logged and never reaches a payment."""

    async def guarded() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background job %s failed", name)

    task = asyncio.get_running_loop().create_task(guarded(), name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def wait_background(timeout: float = 5.0) -> None:
    """Wait for background jobs started so far (tests, and a clean shutdown)."""
    pending = [task for task in _background if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


async def _run(callback: Callable[[], Any]) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


def after_response(callback: Callable[[], Any], name: str = "after-response") -> bool:
    """Run `callback` once the response has been sent. Returns True if it was queued behind a
    request, False if there was no request and it was started in the background right away.

    Live updates, error reporting, AI and timing records go through here: none of them may sit
    between the buyer's click and PayPal's answer.
    """
    timer = _current.get()
    if timer is not None:
        timer.after.append(callback)
        return True
    spawn(_run(callback), name)
    return False


async def run_after_response(timer: StepTimer) -> None:
    """Start the queued callbacks of a finished request, each as its own background job.

    They are started, not awaited: a slow job (a reconcile that waits 0.3, 1, 2, 4 and 8
    seconds) must not keep the request open, and one failing job never stops the next. The
    request's timer is cleared first so a job cannot add steps to a request that is over.
    """
    _current.set(None)
    for callback in timer.after:
        spawn(_run(callback), "after-response")


# ---------------------------------------------------------------------- saving rows
async def record_timings(session: Any, intent_id: str, timer: StepTimer) -> int:
    """Write the timer's steps to payment_timings. Always call this AFTER the response."""
    from backend.models.timing import insert_timings

    rows = [
        {"intent_id": intent_id, "step": s.name, "kind": s.kind, "ms": s.ms} for s in timer.steps
    ]
    if not rows:
        return 0
    await insert_timings(session, rows)
    await session.commit()
    return len(rows)


async def persist(session_factory: Callable[[], Any] | None, timer: StepTimer) -> None:
    """Save a finished timer, if it belongs to a payment. Never raises."""
    if session_factory is None or not timer.intent_id or not timer.steps:
        return
    try:
        async with session_factory() as session:
            await record_timings(session, timer.intent_id, timer)
    except Exception:
        log.warning("could not save timings for a payment", exc_info=True)


# ---------------------------------------------------------------------- statistics
def percentile(values: Iterable[float], pct: float) -> float | None:
    """Linear-interpolated percentile (the same rule as PostgreSQL's percentile_cont)."""
    ordered = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not ordered:
        return None
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    position = pct / 100 * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def per_payment(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Add up the steps of each payment by kind: {intent: {ours, provider, human}}."""
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        kind = row["kind"]
        if kind not in COUNTED_KINDS:
            continue
        entry = totals.setdefault(str(row["intent_id"]), {k: 0.0 for k in COUNTED_KINDS})
        entry[kind] += float(row["ms"])
    return totals


def summarize_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """p50 and p95 of every step, and of the overhead (`ours` per payment), from saved rows.

    `overhead` is SlothPay's own time added to one whole payment (all of its requests). PayPal's
    time and the buyer's time are never part of it.
    """
    items = [dict(r) for r in rows]
    by_step: dict[str, dict[str, Any]] = {}
    for row in items:
        entry = by_step.setdefault(row["step"], {"kind": row["kind"], "values": []})
        entry["values"].append(float(row["ms"]))
    steps = {
        name: {
            "kind": entry["kind"],
            "count": len(entry["values"]),
            "p50_ms": _r(percentile(entry["values"], 50)),
            "p95_ms": _r(percentile(entry["values"], 95)),
        }
        for name, entry in sorted(by_step.items())
    }
    totals = per_payment(items)
    result: dict[str, Any] = {"label": LABEL, "payments": len(totals), "steps": steps}
    for kind in COUNTED_KINDS:
        values = [t[kind] for t in totals.values()]
        result[kind] = {
            "p50_ms": _r(percentile(values, 50)),
            "p95_ms": _r(percentile(values, 95)),
        }
    result["overhead"] = result[OURS]
    return result