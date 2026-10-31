"""Task 8 unit tests: the bounded loop, the rescue plan and the execute guard. No database."""

import pytest

from backend.reliability.reconcile import (
    RECONCILE_DELAYS,
    Reconciler,
    StatusReconciler,
    StepOutcome,
)
from backend.reliability.rescue import build_rescue_plan, execute_rescue_action


class Sleeps:
    def __init__(self):
        self.waits = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


def scripted_step(*outcomes):
    calls = []
    queue = list(outcomes)

    async def step(intent_id):
        calls.append(intent_id)
        return queue.pop(0)

    step.calls = calls
    return step


# ------------------------------------------------------------------ the bounded loop
def test_the_waits_are_exactly_the_planned_ones():
    assert RECONCILE_DELAYS == (0.3, 1.0, 2.0, 4.0, 8.0)


async def test_five_reads_that_prove_nothing_end_exhausted_and_never_a_sixth():
    step = scripted_step(*[StepOutcome("UNKNOWN", "UNCERTAIN")] * 5)
    sleeps = Sleeps()
    result = await StatusReconciler(step, sleep=sleeps).reconcile_detailed("p1")
    assert result.exhausted and result.status == "UNCERTAIN" and result.checks == 5
    assert not result.retry_allowed
    assert sleeps.waits == [0.3, 1.0, 2.0, 4.0, 8.0]
    assert len(step.calls) == 5


async def test_the_loop_stops_at_the_first_proof():
    step = scripted_step(
        StepOutcome("UNKNOWN", "UNCERTAIN"), StepOutcome("EVIDENCE_APPLIED", "CAPTURED")
    )
    sleeps = Sleeps()
    result = await StatusReconciler(step, sleep=sleeps).reconcile_detailed("p1")
    assert (result.status, result.checks, result.exhausted) == ("CAPTURED", 2, False)
    assert sleeps.waits == [0.3, 1.0]


async def test_not_occurred_allows_retry_and_reports_the_earlier_status():
    step = scripted_step(StepOutcome("NOT_OCCURRED", "APPROVED"))
    result = await StatusReconciler(step, sleep=Sleeps()).reconcile_detailed("p1")
    assert result.status == "APPROVED" and result.retry_allowed and not result.exhausted


@pytest.mark.parametrize("decision", ["NO_ATTEMPT", "AMBIGUOUS", "FINAL_STATE", "CONFLICT"])
async def test_decisions_that_need_no_more_reads_stop_without_exhausting(decision):
    step = scripted_step(StepOutcome(decision, "UNCERTAIN"))
    result = await StatusReconciler(step, sleep=Sleeps()).reconcile_detailed("p1")
    assert not result.exhausted and not result.retry_allowed and len(step.calls) == 1


async def test_an_unrecognised_decision_is_never_treated_as_proof():
    step = scripted_step(StepOutcome("SOMETHING_NEW", "UNCERTAIN"))
    result = await StatusReconciler(step, sleep=Sleeps()).reconcile_detailed("p1")
    assert result.status == "UNCERTAIN" and result.decision == "AMBIGUOUS"
    assert not result.retry_allowed


async def test_reconcile_returns_just_the_status_and_matches_the_protocol():
    reconciler: Reconciler = StatusReconciler(
        scripted_step(StepOutcome("EVIDENCE_APPLIED", "VOIDED")), sleep=Sleeps()
    )
    assert await reconciler.reconcile("p1") == "VOIDED"


def test_more_than_five_reads_or_negative_waits_are_refused():
    step = scripted_step()
    with pytest.raises(ValueError):
        StatusReconciler(step, delays=(0.1,) * 6)
    with pytest.raises(ValueError):
        StatusReconciler(step, delays=())
    with pytest.raises(ValueError):
        StatusReconciler(step, delays=(-1,))


# ------------------------------------------------------------------ the rescue plan
def plan(status, path="SMARTPAY", error=None, provider=None, **flags):
    return build_rescue_plan(
        {"status": status, "path": path, **flags},
        {"error_class": error} if error else None,
        provider,
    )


@pytest.mark.parametrize(
    ("status", "error", "extra", "recommended"),
    [
        ("UNCERTAIN", None, {}, "RECONCILE"),
        ("UNCERTAIN", None, {"reconcile_exhausted": True}, "CONTACT_SUPPORT"),
        ("FAILED", "REAPPROVE", {}, "ASK_BUYER_TO_APPROVE"),
        ("FAILED", "SWITCH_METHOD", {}, "ASK_BUYER_NEW_METHOD"),
        ("FAILED", "ESCALATE", {}, "CONTACT_SUPPORT"),
        ("FAILED", "OUR_BUG", {}, "CONTACT_SUPPORT"),
        ("CAPTURE_PENDING", None, {}, "WAIT"),
        ("ORDER_CREATED", None, {}, "WAIT"),
        ("CAPTURED", None, {}, "NONE"),
        ("VOIDED", None, {}, "NONE"),
        ("CANCELLED", None, {}, "NONE"),
    ],
)
def test_the_recommended_action_is_deterministic(status, error, extra, recommended):
    first = plan(status, error=error, **extra)
    assert first["recommended_action"] == recommended
    assert first == plan(status, error=error, **extra)
    assert first["recommended_action"] in first["allowed_actions"]


def test_plan_shape_and_needs_human():
    result = plan("UNCERTAIN", reconcile_exhausted=True, provider="APPROVED")
    assert set(result) == {
        "display_status",
        "facts",
        "allowed_actions",
        "recommended_action",
        "needs_human",
    }
    assert result["display_status"] == "ACTION_NEEDED"
    assert result["allowed_actions"] == ["CONTACT_SUPPORT"]
    assert result["needs_human"] is True
    assert all(isinstance(item, str) for item in result["facts"])
    assert any("APPROVED" in item for item in result["facts"])
    assert plan("CAPTURE_PENDING")["needs_human"] is False


def test_retry_needs_proof_and_is_never_offered_while_uncertain():
    assert "RETRY_SAME_REQUEST" not in plan("APPROVED")["allowed_actions"]
    assert "RETRY_SAME_REQUEST" in plan("APPROVED", retry_allowed=True)["allowed_actions"]
    assert "RETRY_SAME_REQUEST" in plan("CAPTURE_REQUESTED", retry_allowed=True)["allowed_actions"]
    assert plan("UNCERTAIN", retry_allowed=True)["allowed_actions"] == ["RECONCILE"]
    assert plan("CAPTURED", retry_allowed=True)["allowed_actions"] == ["NONE"]


def test_the_plan_never_contains_secrets():
    text = str(
        build_rescue_plan(
            {"status": "UNCERTAIN", "path": "SMARTPAY", "token": "Bearer abc", "secret": "s3"},
            {"error_class": "RETRY", "message": "Bearer abc"},
            "APPROVED",
        )
    )
    assert "abc" not in text and "s3" not in text


# ------------------------------------------------------------ the execute guard
class FakeService:
    def __init__(self, intent):
        self.intent = intent
        self.events = []
        self.ran = []

    async def load_intent(self, intent_id):
        return self.intent

    async def reconcile(self, intent_id):
        self.ran.append("reconcile")
        return {"state": "CAPTURED"}

    async def retry_same_request(self, intent_id):
        self.ran.append("retry")
        return {"state": "CAPTURED"}

    async def run_operation(self, intent_id, action):
        self.ran.append(action)
        return {"state": "CAPTURED"}

    async def audit(self, intent_id, event_type, evidence):
        self.events.append((event_type, evidence))


async def test_an_action_outside_the_allowed_list_is_rejected_and_nothing_runs():
    service = FakeService({"status": "CAPTURED", "path": "SMARTPAY"})
    for action in ("CAPTURE", "VOID", "RECONCILE", "RETRY_SAME_REQUEST", "DELETE_EVERYTHING", None):
        assert await execute_rescue_action(service, "p1", action) == {
            "error": "Action not permitted"
        }
    assert service.ran == []
    assert all(name != "RESCUE_ACTION_EXECUTED" for name, _ in service.events)


async def test_retry_is_rejected_while_the_intent_is_still_uncertain():
    service = FakeService({"status": "UNCERTAIN", "path": "SMARTPAY", "retry_allowed": True})
    assert await execute_rescue_action(service, "p1", "RETRY_SAME_REQUEST") == {
        "error": "Action not permitted"
    }
    assert service.ran == []


async def test_a_permitted_action_runs_and_both_audits_are_written_without_secrets():
    service = FakeService({"status": "UNCERTAIN", "path": "SMARTPAY", "secret": "s3cr3t"})
    outcome = await execute_rescue_action(service, "p1", "RECONCILE")
    assert outcome == {"action": "RECONCILE", "result": {"state": "CAPTURED"}}
    assert service.ran == ["reconcile"]
    names = [name for name, _ in service.events]
    assert names == ["RESCUE_PLAN_BUILT", "RESCUE_ACTION_EXECUTED"]
    assert service.events[0][1] == {
        "allowed_actions": ["RECONCILE"],
        "recommended_action": "RECONCILE",
    }
    assert "s3cr3t" not in str(service.events)


async def test_a_proven_retry_runs_only_with_the_proof_flag():
    service = FakeService({"status": "APPROVED", "path": "SMARTPAY", "retry_allowed": True})
    outcome = await execute_rescue_action(service, "p1", "RETRY_SAME_REQUEST")
    assert outcome["action"] == "RETRY_SAME_REQUEST" and service.ran == ["retry"]


async def test_wait_and_contact_support_run_nothing():
    service = FakeService({"status": "CAPTURE_PENDING", "path": "SMARTPAY"})
    outcome = await execute_rescue_action(service, "p1", "WAIT")
    assert outcome["result"]["executed"] is False and service.ran == []