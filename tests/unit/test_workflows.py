"""Task 9 workflow tests: signals, deadline, duplicate start, clamping, reconcile schedule.

The workflows run on Temporal's time-skipping test server (a day-long wait takes milliseconds).
If that server cannot be downloaded (no internet), point the tests at any running Temporal
dev server instead:  TEMPORAL_TEST_ADDRESS=localhost:7233 pytest tests/unit/test_workflows.py
(real clock, so the tests use short deadlines there). With neither, the Temporal tests skip and
the pure-logic tests still run. Activities are fakes: no database, no PayPal.
"""

import ast
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.worker import Worker

from backend.config.settings import get_settings
from backend.reliability import reconcile as task8_reconcile
from backend.workflows import common
from backend.workflows.client import TemporalWorkflowStarter
from backend.workflows.common import (
    ACTIVITY_MARK_EXPIRED,
    ACTIVITY_RECONCILE,
    ACTIVITY_RESOLVE_CONDITION,
    clamp_deadline,
    clamp_wait_seconds,
    conditional_workflow_id,
    parse_deadline,
    reconcile_workflow_id,
)
from backend.workflows.conditional_pay import ConditionalPayWorkflow
from backend.workflows.reconcile_workflow import ReconcileWorkflow, SilenceWatcherWorkflow
from backend.workflows.starter import NoopWorkflowStarter

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------- pure logic (no Temporal)
def test_wait_is_never_negative_and_never_beyond_the_ceiling():
    assert clamp_wait_seconds(NOW, NOW + timedelta(hours=1), 48) == 3600
    assert clamp_wait_seconds(NOW, NOW + timedelta(hours=500), 48) == 48 * 3600
    assert clamp_wait_seconds(NOW, NOW - timedelta(hours=1), 48) == 0
    assert clamp_wait_seconds(NOW, NOW + timedelta(hours=5), 0) == 0
    assert clamp_wait_seconds(NOW, NOW + timedelta(hours=5), -3) == 0


def test_an_unreadable_deadline_means_no_waiting_so_the_funds_are_voided():
    for bad in ("", "soon", "2026-13-45", None, 12345):
        assert parse_deadline(bad) is None
    assert clamp_wait_seconds(NOW, parse_deadline("soon"), 48) == 0


def test_a_naive_deadline_is_read_as_utc():
    assert parse_deadline("2026-11-01T13:00:00") == NOW + timedelta(hours=1)


def test_clamp_deadline_pulls_a_far_deadline_in():
    far = (NOW + timedelta(hours=500)).isoformat()
    assert parse_deadline(clamp_deadline(NOW, far, 48)) == NOW + timedelta(hours=48)
    near = (NOW + timedelta(minutes=5)).isoformat()
    assert parse_deadline(clamp_deadline(NOW, near, 48)) == NOW + timedelta(minutes=5)


def test_workflow_ids_are_deterministic():
    assert conditional_workflow_id("abc") == "conditional-abc"
    assert reconcile_workflow_id("abc") == "reconcile-abc"
    assert common.silence_workflow_id("abc") == "silence-abc"


def test_the_reconcile_schedule_matches_task_8():
    assert common.RECONCILE_DELAYS == task8_reconcile.RECONCILE_DELAYS == (0.3, 1.0, 2.0, 4.0, 8.0)
    stops = task8_reconcile._STOP_DECISIONS
    assert common.RECONCILE_STOP_DECISIONS == stops
    assert common.RECONCILE_RETRY_DECISIONS == {"UNKNOWN", "WAITING"}
    assert not stops & common.RECONCILE_RETRY_DECISIONS


async def test_the_noop_starter_does_nothing_and_says_so():
    starter = NoopWorkflowStarter()
    assert await starter.start("p1", NOW.isoformat()) is False
    assert await starter.signal("p1", True, {}) is False


@pytest.mark.parametrize("name", ["conditional_pay.py", "reconcile_workflow.py"])
def test_workflow_files_use_no_nondeterministic_calls(name):
    tree = ast.parse((Path(common.__file__).parent / name).read_text())
    modules, calls = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute):
            calls.add(f"{getattr(node.value, 'id', '')}.{node.attr}")
    banned_modules = {"random", "uuid", "time", "httpx", "requests", "sqlalchemy", "secrets"}
    banned_modules |= {"os", "pathlib", "dotenv", "pydantic_settings"}
    assert not modules & banned_modules, f"{name} must stay deterministic"
    assert not any(
        isinstance(n, ast.ImportFrom)
        and n.module
        and n.module.startswith("backend.")
        and not n.module.startswith("backend.workflows")
        for n in ast.walk(tree)
    ), f"{name} may import only backend.workflows modules (no settings, database or PayPal)"
    assert not calls & {"datetime.now", "datetime.utcnow", "time.time", "os.urandom"}


# ------------------------------------------------------------------ Temporal environment
# The `temporal` fixture lives in tests/conftest.py (shared with the database tests).


class FakeActivities:
    """Records every call. No database, no PayPal."""

    def __init__(self, *, resolve=None, reconcile_decisions=None):
        self.resolved: list[tuple] = []
        self.expired: list[tuple] = []
        self.reconciled: list[str] = []
        self._resolve = resolve or {
            "state": "DONE",
            "action": "NONE",
            "needs_reconciliation": False,
        }
        self._decisions = list(reconcile_decisions or [])

    def all(self):
        outer = self

        @activity.defn(name=ACTIVITY_RESOLVE_CONDITION)
        async def resolve_condition(intent_id: str, verified: bool, evidence: dict, reason: str):
            outer.resolved.append((intent_id, verified, reason))
            action = "CAPTURE" if verified else "VOID"
            return {**outer._resolve, "action": action}

        @activity.defn(name=ACTIVITY_MARK_EXPIRED)
        async def mark_condition_expired(intent_id: str, reason: str):
            outer.expired.append((intent_id, reason))
            return {"condition_status": "EXPIRED"}

        @activity.defn(name=ACTIVITY_RECONCILE)
        async def reconcile(intent_id: str):
            outer.reconciled.append(intent_id)
            decision = outer._decisions.pop(0) if outer._decisions else "NO_ATTEMPT"
            return {"decision": decision, "status": "CAPTURED"}

        return [resolve_condition, mark_condition_expired, reconcile]


def worker_for(env, fake, queue):
    return Worker(
        env.client,
        task_queue=queue,
        workflows=[ConditionalPayWorkflow, ReconcileWorkflow, SilenceWatcherWorkflow],
        activities=fake.all(),
    )


def deadline_in(skipping, *, far=3600, near=2):
    return (datetime.now(UTC) + timedelta(seconds=far if skipping else near)).isoformat()


async def run_flow(temporal, fake, action):
    """Start a worker, build a starter, run `action(starter, intent_id, skipping)`."""
    env, skipping = temporal
    queue = f"test-{uuid.uuid4().hex}"
    intent_id = str(uuid.uuid4())
    starter = TemporalWorkflowStarter(env.client, task_queue=queue, max_hours=48)
    async with worker_for(env, fake, queue):
        return await action(starter, intent_id, skipping), intent_id


async def result_of(env, intent_id, *, skip_seconds=0):
    if skip_seconds and env.supports_time_skipping:
        await env.sleep(timedelta(seconds=skip_seconds))
    return await env.client.get_workflow_handle(conditional_workflow_id(intent_id)).result()


# --------------------------------------------------------------------- the five spec cases
async def test_signal_verified_requests_the_capture_once(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        assert await starter.start(intent_id, deadline_in(skipping, far=3600, near=60))
        assert await starter.signal(intent_id, True, {"proof": "order shipped"})
        return await result_of(env, intent_id)

    result, intent_id = await run_flow(temporal, fake, action)
    assert fake.resolved == [(intent_id, True, "verified")]
    assert fake.expired == []
    assert result["outcome"] == "CAPTURE" and result["reason"] == "verified"
    assert fake.reconciled == [intent_id], "one safety check after the capture"


async def test_signal_failed_requests_the_void(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        await starter.start(intent_id, deadline_in(skipping, far=3600, near=60))
        await starter.signal(intent_id, False, {"proof": "not met"})
        return await result_of(env, intent_id)

    result, intent_id = await run_flow(temporal, fake, action)
    assert fake.resolved == [(intent_id, False, "failed")]
    assert result["outcome"] == "VOID"


async def test_no_signal_until_the_deadline_voids_and_never_captures(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        await starter.start(intent_id, deadline_in(skipping))
        return await result_of(env, intent_id, skip_seconds=3700)

    result, intent_id = await run_flow(temporal, fake, action)
    assert fake.expired == [(intent_id, "deadline")]
    assert fake.resolved == [(intent_id, False, "deadline")]
    assert result["outcome"] == "VOID" and result["reason"] == "deadline"
    assert all(verified is False for _, verified, _ in fake.resolved)


async def test_a_second_start_with_the_same_id_does_not_start_a_second_workflow(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        deadline = deadline_in(skipping, far=3600, near=60)
        assert await starter.start(intent_id, deadline) is True
        assert await starter.start(intent_id, deadline) is False  # success, but a no-op
        await starter.signal(intent_id, True, {})
        await result_of(env, intent_id)
        assert await starter.start(intent_id, deadline) is False  # still a no-op after it ended

    _, intent_id = await run_flow(temporal, fake, action)
    assert len(fake.resolved) == 1


async def test_the_deadline_is_clamped_to_the_configured_maximum(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        # Started directly with a deadline an hour away but a 0 hour maximum: the workflow
        # itself clamps, so it must not wait at all.
        await env.client.start_workflow(
            "ConditionalPayWorkflow",
            args=[intent_id, deadline_in(True, far=3600), 0],
            id=conditional_workflow_id(intent_id),
            task_queue=starter._task_queue,
        )
        return await result_of(env, intent_id)

    result, _ = await run_flow(temporal, fake, action)
    assert result["reason"] == "deadline" and result["outcome"] == "VOID"


async def test_the_starter_passes_its_maximum_to_the_workflow(temporal):
    env, skipping = temporal
    fake = FakeActivities()
    queue = f"test-{uuid.uuid4().hex}"
    intent_id = str(uuid.uuid4())
    starter = TemporalWorkflowStarter(env.client, task_queue=queue, max_hours=0)
    async with worker_for(env, fake, queue):
        await starter.start(intent_id, deadline_in(True, far=3600))
        result = await result_of(env, intent_id)
    assert result["reason"] == "deadline" and fake.resolved == [(intent_id, False, "deadline")]


async def test_the_workflow_works_with_a_dotenv_file_present(temporal, tmp_path, monkeypatch):
    """Regression: the workflow once read settings, which opens .env, which the Temporal
    sandbox forbids. Run from a folder that has a .env and with settings never loaded."""
    env, skipping = temporal
    (tmp_path / ".env").write_text("CONDITION_MAX_HOURS=12\n")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        await starter.start(intent_id, deadline_in(skipping, far=3600, near=60))
        await starter.signal(intent_id, True, {})
        # A stuck workflow would otherwise wait over a minute before failing.
        return await asyncio.wait_for(result_of(env, intent_id), timeout=30)

    result, _ = await run_flow(temporal, fake, action)
    assert result["outcome"] == "CAPTURE"


# ----------------------------------------------------------------------- more safety cases
async def test_cancel_voids(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        await starter.start(intent_id, deadline_in(skipping, far=3600, near=60))
        assert await starter.cancel(intent_id)
        return await result_of(env, intent_id)

    result, intent_id = await run_flow(temporal, fake, action)
    assert result["reason"] == "cancelled" and fake.resolved == [(intent_id, False, "cancelled")]


async def test_only_a_real_true_counts_as_verified(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        await starter.start(intent_id, deadline_in(skipping, far=3600, near=60))
        handle = env.client.get_workflow_handle(conditional_workflow_id(intent_id))
        await handle.signal("condition_result", args=["yes", {}])  # truthy, but not True
        return await result_of(env, intent_id)

    result, intent_id = await run_flow(temporal, fake, action)
    assert fake.resolved == [(intent_id, False, "failed")] and result["outcome"] == "VOID"


async def test_the_first_decision_wins(temporal):
    env, skipping = temporal
    fake = FakeActivities()
    queue = f"test-{uuid.uuid4().hex}"
    intent_id = str(uuid.uuid4())
    starter = TemporalWorkflowStarter(env.client, task_queue=queue, max_hours=48)
    # Both signals are queued BEFORE any worker exists, so the workflow receives them in one
    # step: the first (failed) must win over the second (verified).
    await starter.start(intent_id, deadline_in(skipping, far=3600, near=60))
    await starter.signal(intent_id, False, {})
    await starter.signal(intent_id, True, {})
    async with worker_for(env, fake, queue):
        await result_of(env, intent_id)
    assert fake.resolved == [(intent_id, False, "failed")]


async def test_an_invalid_deadline_voids_instead_of_waiting_forever(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        await env.client.start_workflow(
            "ConditionalPayWorkflow",
            args=[intent_id, "not-a-date"],
            id=conditional_workflow_id(intent_id),
            task_queue=starter._task_queue,
        )
        return await result_of(env, intent_id)

    result, intent_id = await run_flow(temporal, fake, action)
    assert result["reason"] == "invalid_deadline" and result["outcome"] == "VOID"


async def test_signalling_a_finished_or_missing_workflow_is_reported_not_raised(temporal):
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        return await starter.signal(intent_id, True, {})

    delivered, _ = await run_flow(temporal, fake, action)
    assert delivered is False


# --------------------------------------------------------------------- reconcile workflows
async def test_an_unknown_outcome_hands_over_to_the_reconcile_workflow(temporal):
    env, _ = temporal
    fake = FakeActivities(
        resolve={"state": "UNCERTAIN", "action": "CAPTURE", "needs_reconciliation": True},
        reconcile_decisions=["NO_ATTEMPT", "UNKNOWN", "EVIDENCE_APPLIED"],
    )

    async def action(starter, intent_id, skipping):
        await starter.start(intent_id, deadline_in(skipping, far=3600, near=60))
        await starter.signal(intent_id, True, {})
        outer = await result_of(env, intent_id)
        child = await env.client.get_workflow_handle(reconcile_workflow_id(intent_id)).result()
        return outer, child

    (outer, child), intent_id = await run_flow(temporal, fake, action)
    assert outer["reconcile_started"] is True and outer["needs_reconciliation"] is True
    assert child["decision"] == "EVIDENCE_APPLIED" and child["exhausted"] is False
    assert child["checks"] >= 1


async def test_five_reads_that_prove_nothing_end_exhausted(temporal):
    env, _ = temporal
    fake = FakeActivities(reconcile_decisions=["UNKNOWN"] * 5)

    async def action(starter, intent_id, skipping):
        await starter.start_reconcile(intent_id)
        await starter.start_reconcile(intent_id)  # a second start while running is a no-op
        return await env.client.get_workflow_handle(reconcile_workflow_id(intent_id)).result()

    result, intent_id = await run_flow(temporal, fake, action)
    assert result["exhausted"] is True and result["checks"] == 5
    assert fake.reconciled == [intent_id] * 5


async def test_the_reconcile_workflow_stops_at_the_first_proof(temporal):
    env, _ = temporal
    fake = FakeActivities(reconcile_decisions=["UNKNOWN", "EVIDENCE_APPLIED"])

    async def action(starter, intent_id, skipping):
        await starter.start_reconcile(intent_id)
        return await env.client.get_workflow_handle(reconcile_workflow_id(intent_id)).result()

    result, intent_id = await run_flow(temporal, fake, action)
    assert result["checks"] == 2 and result["exhausted"] is False
    assert len(fake.reconciled) == 2


async def test_silence_watcher_reconciles_once_when_no_webhook_arrives(temporal):
    env, _ = temporal
    fake = FakeActivities(reconcile_decisions=["EVIDENCE_APPLIED"])

    async def action(starter, intent_id, skipping):
        assert await starter.watch_capture(intent_id, wait_seconds=1.0)
        assert not await starter.watch_capture(intent_id, wait_seconds=1.0)
        return await env.client.get_workflow_handle(common.silence_workflow_id(intent_id)).result()

    result, intent_id = await run_flow(temporal, fake, action)
    assert result["reconciled"] is True and fake.reconciled == [intent_id]


async def test_silence_watcher_does_nothing_when_the_webhook_arrives(temporal):
    env, _ = temporal
    fake = FakeActivities()

    async def action(starter, intent_id, skipping):
        await starter.watch_capture(intent_id, wait_seconds=60.0)
        assert await starter.webhook_confirmed(intent_id)
        return await env.client.get_workflow_handle(common.silence_workflow_id(intent_id)).result()

    result, _ = await run_flow(temporal, fake, action)
    assert result["webhook_confirmed"] is True and fake.reconciled == []