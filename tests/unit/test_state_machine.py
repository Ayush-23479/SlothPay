

from itertools import product

import pytest

from backend.state_machine.policy import allowed_actions, display_status_for
from backend.state_machine.states import (
    ACTIONS,
    ALL_STATUSES,
    DISPLAY_STATUSES,
    FINAL_STATUSES,
    PATHS,
    is_final,
    normalize_path,
    normalize_status,
)
from backend.state_machine.transitions import (
    next_statuses,
    resolve_uncertain,
    validate_transition,
)

SMART = "SMARTPAY"
COND = "CONDITIONALPAY"

COMMON = {
    ("INTENT_CREATED", "ORDER_CREATED"),
    ("INTENT_CREATED", "FAILED"),
    ("INTENT_CREATED", "CANCELLED"),
    ("INTENT_CREATED", "UNCERTAIN"),  # create_order can be left uncertain
    ("ORDER_CREATED", "AWAITING_APPROVAL"),
    ("ORDER_CREATED", "FAILED"),
    ("ORDER_CREATED", "UNCERTAIN"),
    ("AWAITING_APPROVAL", "APPROVED"),
    ("AWAITING_APPROVAL", "CANCELLED"),
    ("AWAITING_APPROVAL", "FAILED"),
    ("CAPTURE_REQUESTED", "CAPTURED"),
    ("CAPTURE_REQUESTED", "CAPTURE_PENDING"),
    ("CAPTURE_REQUESTED", "FAILED"),
    ("CAPTURE_REQUESTED", "UNCERTAIN"),
    ("CAPTURE_PENDING", "CAPTURED"),
    ("CAPTURE_PENDING", "FAILED"),
}
SMART_ONLY = {("APPROVED", "CAPTURE_REQUESTED")}
COND_ONLY = {
    ("APPROVED", "AUTHORIZE_REQUESTED"),
    ("AUTHORIZE_REQUESTED", "AUTHORIZED"),
    ("AUTHORIZE_REQUESTED", "FAILED"),
    ("AUTHORIZE_REQUESTED", "UNCERTAIN"),
    ("AUTHORIZED", "AWAITING_CONDITION"),
    ("AWAITING_CONDITION", "CAPTURE_REQUESTED"),
    ("AWAITING_CONDITION", "VOID_REQUESTED"),
    ("VOID_REQUESTED", "VOIDED"),
    ("VOID_REQUESTED", "FAILED"),
    ("VOID_REQUESTED", "UNCERTAIN"),
}
EXPECTED = {SMART: COMMON | SMART_ONLY, COND: COMMON | COND_ONLY}


# ------------------------------------------------------------------ transitions
@pytest.mark.parametrize("path", PATHS)
def test_every_pair_matches_the_design_table(path):
    for cur, nxt in product(sorted(ALL_STATUSES), repeat=2):
        if cur == "UNCERTAIN":
            continue  # covered by the UNCERTAIN tests below
        ok, _ = validate_transition(path, cur, nxt)
        assert ok == ((cur, nxt) in EXPECTED[path]), f"{path}: {cur} -> {nxt}"


@pytest.mark.parametrize(("cur", "nxt"), sorted(COMMON | SMART_ONLY))
def test_legal_smartpay_moves_pass(cur, nxt):
    assert validate_transition(SMART, cur, nxt) == (True, "")


@pytest.mark.parametrize(("cur", "nxt"), sorted(COMMON | COND_ONLY))
def test_legal_conditionalpay_moves_pass(cur, nxt):
    assert validate_transition(COND, cur, nxt) == (True, "")


def test_failure_message_format():
    ok, message = validate_transition(SMART, "INTENT_CREATED", "CAPTURED")
    assert not ok
    assert message == "Invalid transition: INTENT_CREATED -> CAPTURED for path SMARTPAY"


@pytest.mark.parametrize(
    "target",
    ["AUTHORIZE_REQUESTED", "AUTHORIZED", "AWAITING_CONDITION", "VOID_REQUESTED", "VOIDED"],
)
def test_smartpay_can_never_reach_authorize_or_void_statuses(target):
    for cur in ALL_STATUSES:
        assert validate_transition(SMART, cur, target, pending_operation=None)[0] is False
    # not even through UNCERTAIN resolution
    for op in ("create_order", "authorize", "capture", "void"):
        assert validate_transition(SMART, "UNCERTAIN", target, pending_operation=op)[0] is False


def test_conditionalpay_cannot_capture_straight_from_approved():
    assert validate_transition(COND, "APPROVED", "CAPTURE_REQUESTED")[0] is False
    assert validate_transition(COND, "AUTHORIZED", "CAPTURE_REQUESTED")[0] is False
    assert validate_transition(COND, "AWAITING_CONDITION", "CAPTURE_REQUESTED")[0] is True


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("final", sorted(FINAL_STATUSES))
def test_final_statuses_have_no_exits(path, final):
    for nxt in ALL_STATUSES:
        assert validate_transition(path, final, nxt)[0] is False
        assert validate_transition(path, final, nxt, pending_operation="capture")[0] is False
    assert next_statuses(path, final) == frozenset()


def test_a_status_cannot_move_to_itself():
    for path, status in product(PATHS, ALL_STATUSES):
        assert validate_transition(path, status, status)[0] is False


def test_no_backward_moves_between_ordinary_statuses():
    assert validate_transition(SMART, "CAPTURE_REQUESTED", "APPROVED")[0] is False
    assert validate_transition(COND, "AWAITING_CONDITION", "AUTHORIZED")[0] is False
    assert validate_transition(SMART, "AWAITING_APPROVAL", "ORDER_CREATED")[0] is False


def test_unknown_path_or_status_is_refused():
    assert validate_transition("MAGICPAY", "APPROVED", "CAPTURE_REQUESTED")[0] is False
    assert validate_transition(SMART, "NOPE", "CAPTURED")[0] is False
    assert validate_transition(SMART, "APPROVED", "NOPE")[0] is False
    assert validate_transition(None, "APPROVED", "CAPTURE_REQUESTED")[0] is False
    assert validate_transition(SMART, None, "CAPTURED")[0] is False


def test_next_statuses_agrees_with_validate_transition():
    for path, cur in product(PATHS, ALL_STATUSES):
        if cur == "UNCERTAIN":
            continue
        expected = {n for n in ALL_STATUSES if validate_transition(path, cur, n)[0]}
        assert set(next_statuses(path, cur)) == expected


# ---------------------------------------------------------------------- UNCERTAIN
def test_uncertain_needs_a_pending_operation_to_leave():
    for path, nxt in product(PATHS, ALL_STATUSES):
        ok, message = validate_transition(path, "UNCERTAIN", nxt)
        assert ok is False
        assert message.startswith("Invalid transition: UNCERTAIN ->")


RESOLUTION_CASES = [
    # (path, pending_operation, provider_status, expected)
    (SMART, "capture", "COMPLETED", "CAPTURED"),
    (SMART, "capture", "PENDING", "CAPTURE_PENDING"),
    (SMART, "capture", "DECLINED", "FAILED"),
    (SMART, "capture", "DENIED", "FAILED"),
    (SMART, "capture", "APPROVED", "APPROVED"),  # capture did not happen
    (SMART, "create_order", "CREATED", "ORDER_CREATED"),
    (SMART, "create_order", "NOT_FOUND", "INTENT_CREATED"),
    (COND, "capture", "COMPLETED", "CAPTURED"),
    (COND, "capture", "AUTHORIZED", "AWAITING_CONDITION"),  # capture did not happen
    (COND, "authorize", "AUTHORIZED", "AUTHORIZED"),
    (COND, "authorize", "APPROVED", "APPROVED"),  # authorize did not happen
    (COND, "authorize", "DECLINED", "FAILED"),
    (COND, "void", "VOIDED", "VOIDED"),
    (COND, "void", "AUTHORIZED", "AWAITING_CONDITION"),  # void did not happen
    (COND, "create_order", "PAYER_ACTION_REQUIRED", "ORDER_CREATED"),
]


@pytest.mark.parametrize(("path", "op", "provider", "expected"), RESOLUTION_CASES)
def test_resolve_uncertain_cases(path, op, provider, expected):
    assert resolve_uncertain(path, op, provider) == expected
    # and the same move is accepted by validate_transition
    ok, _ = validate_transition(path, "UNCERTAIN", expected, pending_operation=op)
    assert ok is True


def test_resolve_uncertain_is_case_and_space_insensitive():
    assert resolve_uncertain("smartpay", " Capture ", " completed ") == "CAPTURED"
    assert resolve_uncertain("SMART", "capture", "COMPLETED") == "CAPTURED"  # alias


@pytest.mark.parametrize(
    ("path", "op", "provider"),
    [
        (SMART, "capture", "SOMETHING_NEW"),
        (SMART, "capture", ""),
        (SMART, "authorize", "AUTHORIZED"),  # SmartPay never authorizes
        (SMART, "void", "VOIDED"),  # SmartPay never voids
        (COND, "refund", "COMPLETED"),
        ("MAGICPAY", "capture", "COMPLETED"),
        (COND, None, "COMPLETED"),
        (COND, "capture", None),
    ],
)
def test_unrecognised_input_keeps_the_payment_uncertain(path, op, provider):
    assert resolve_uncertain(path, op, provider) == "UNCERTAIN"


def test_uncertain_cannot_jump_to_unrelated_statuses():
    # a capture that is unsure can not become VOIDED or AWAITING_APPROVAL, for example
    for target in ("VOIDED", "AWAITING_APPROVAL", "AUTHORIZED", "CANCELLED", "UNCERTAIN"):
        assert (
            validate_transition(COND, "UNCERTAIN", target, pending_operation="capture")[0] is False
        )


# ------------------------------------------------------------------ display status
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("INTENT_CREATED", "PENDING"),
        ("ORDER_CREATED", "PENDING"),
        ("AWAITING_APPROVAL", "ACTION_NEEDED"),
        ("APPROVED", "PENDING"),
        ("AUTHORIZE_REQUESTED", "PENDING"),
        ("AUTHORIZED", "PENDING"),
        ("AWAITING_CONDITION", "PENDING"),
        ("CAPTURE_REQUESTED", "PENDING"),
        ("CAPTURE_PENDING", "PENDING"),
        ("CAPTURED", "COMPLETED"),
        ("VOID_REQUESTED", "PENDING"),
        ("VOIDED", "CLOSED"),
        ("UNCERTAIN", "PENDING"),
        ("FAILED", "CLOSED"),
        ("CANCELLED", "CLOSED"),
    ],
)
def test_display_status_for_every_status(status, expected):
    assert display_status_for(status) == expected
    assert display_status_for(status, {}) == expected
    assert expected in DISPLAY_STATUSES


def test_every_status_has_a_display_status():
    assert {display_status_for(s) for s in ALL_STATUSES} <= set(DISPLAY_STATUSES)


def test_uncertain_becomes_action_needed_when_reconcile_is_exhausted():
    assert display_status_for("UNCERTAIN", {"reconcile_exhausted": False}) == "PENDING"
    assert display_status_for("UNCERTAIN", {"reconcile_exhausted": True}) == "ACTION_NEEDED"


@pytest.mark.parametrize(
    "error_class", ["SWITCH_METHOD", "FIX_INFO", "REAPPROVE", "OUR_BUG", "ESCALATE"]
)
def test_failed_with_a_known_error_class_needs_action(error_class):
    assert display_status_for("FAILED", {"error_class": error_class}) == "ACTION_NEEDED"


def test_failed_without_a_useful_error_class_is_closed():
    assert display_status_for("FAILED", {"error_class": None}) == "CLOSED"
    assert display_status_for("FAILED", {"error_class": "RETRY"}) == "CLOSED"


def test_unknown_status_is_shown_to_a_human():
    assert display_status_for("???") == "ACTION_NEEDED"
    assert display_status_for(None) == "ACTION_NEEDED"


# ---------------------------------------------------------------- allowed actions
def test_unknown_input_returns_none_action():
    assert allowed_actions("MAGICPAY", "APPROVED", {}) == ["NONE"]
    assert allowed_actions(SMART, "NOPE", {}) == ["NONE"]
    assert allowed_actions(None, None, None) == ["NONE"]


def test_final_statuses_allow_nothing_except_failed_with_an_error_class():
    for path, status in product(PATHS, ("CAPTURED", "VOIDED", "CANCELLED")):
        assert allowed_actions(path, status, {"error_class": "SWITCH_METHOD"}) == ["NONE"]
    assert allowed_actions(SMART, "FAILED", {}) == ["NONE"]
    assert allowed_actions(SMART, "FAILED", {"error_class": "RETRY"}) == ["NONE"]


@pytest.mark.parametrize(
    ("error_class", "expected"),
    [
        ("SWITCH_METHOD", "ASK_BUYER_NEW_METHOD"),
        ("FIX_INFO", "ASK_BUYER_NEW_METHOD"),
        ("REAPPROVE", "ASK_BUYER_TO_APPROVE"),
        ("OUR_BUG", "CONTACT_SUPPORT"),
        ("ESCALATE", "CONTACT_SUPPORT"),
    ],
)
def test_failed_payment_offers_only_the_matching_human_action(error_class, expected):
    assert allowed_actions(SMART, "FAILED", {"error_class": error_class}) == [expected]


def test_uncertain_offers_reconcile_then_only_contact_support():
    assert allowed_actions(SMART, "UNCERTAIN", {}) == ["RECONCILE"]
    assert allowed_actions(COND, "UNCERTAIN", {"reconcile_exhausted": True}) == ["CONTACT_SUPPORT"]


def test_retry_same_request_is_never_offered_while_uncertain():
    ctx = {"retry_allowed": True}
    assert "RETRY_SAME_REQUEST" not in allowed_actions(SMART, "UNCERTAIN", ctx)
    assert "RETRY_SAME_REQUEST" not in allowed_actions(
        SMART, "UNCERTAIN", {**ctx, "reconcile_exhausted": True}
    )


def test_retry_same_request_only_after_reconcile_proved_it_did_not_happen():
    assert "RETRY_SAME_REQUEST" not in allowed_actions(SMART, "APPROVED", {})
    assert "RETRY_SAME_REQUEST" in allowed_actions(SMART, "APPROVED", {"retry_allowed": True})
    assert "RETRY_SAME_REQUEST" in allowed_actions(
        COND, "AWAITING_CONDITION", {"retry_allowed": True}
    )
    assert "RETRY_SAME_REQUEST" in allowed_actions(SMART, "INTENT_CREATED", {"retry_allowed": True})
    assert "RETRY_SAME_REQUEST" not in allowed_actions(SMART, "CAPTURED", {"retry_allowed": True})


def test_smartpay_capture_is_only_offered_from_approved():
    assert allowed_actions(SMART, "APPROVED", {}) == ["CAPTURE"]
    for status in ALL_STATUSES - {"APPROVED"}:
        assert "CAPTURE" not in allowed_actions(SMART, status, {"condition_verified": True})


def test_conditionalpay_approved_waits_for_the_automatic_authorization():
    assert allowed_actions(COND, "APPROVED", {}) == ["WAIT"]


def test_awaiting_condition_never_captures_until_the_condition_is_verified():
    assert allowed_actions(COND, "AWAITING_CONDITION", {}) == ["WAIT", "VOID"]
    assert allowed_actions(COND, "AWAITING_CONDITION", {"condition_verified": False}) == [
        "WAIT",
        "VOID",
    ]
    assert allowed_actions(COND, "AWAITING_CONDITION", {"condition_verified": "yes"}) == [
        "WAIT",
        "VOID",
    ]
    assert allowed_actions(COND, "AWAITING_CONDITION", {"condition_verified": True}) == [
        "CAPTURE",
        "VOID",
    ]


def test_conditional_only_statuses_offer_nothing_on_smartpay():
    for status in (
        "AUTHORIZE_REQUESTED",
        "AUTHORIZED",
        "AWAITING_CONDITION",
        "VOID_REQUESTED",
        "VOIDED",
    ):
        assert allowed_actions(SMART, status, {"condition_verified": True}) == ["NONE"]


def test_awaiting_approval_asks_the_buyer():
    assert allowed_actions(SMART, "AWAITING_APPROVAL", {}) == ["ASK_BUYER_TO_APPROVE"]
    assert allowed_actions(COND, "AWAITING_APPROVAL", {}) == ["ASK_BUYER_TO_APPROVE"]


@pytest.mark.parametrize(
    "status",
    [
        "INTENT_CREATED",
        "ORDER_CREATED",
        "AUTHORIZE_REQUESTED",
        "AUTHORIZED",
        "CAPTURE_REQUESTED",
        "CAPTURE_PENDING",
        "VOID_REQUESTED",
    ],
)
def test_in_flight_statuses_just_wait(status):
    assert allowed_actions(COND, status, {}) == ["WAIT"]


CONTEXTS = [
    {},
    {"condition_verified": True},
    {"condition_verified": False},
    {"retry_allowed": True},
    {"reconcile_exhausted": True},
    {"error_class": "SWITCH_METHOD"},
    {"error_class": "ESCALATE", "reconcile_exhausted": True},
    {"condition_verified": True, "retry_allowed": True, "reconcile_exhausted": True},
]


def test_capture_and_void_are_never_offered_in_the_wrong_place():
    for path, status, ctx in product(PATHS, ALL_STATUSES, CONTEXTS):
        actions = allowed_actions(path, status, ctx)
        assert actions, "never an empty list"
        assert set(actions) <= set(ACTIONS)
        if "CAPTURE" in actions:
            smart_ok = path == SMART and status == "APPROVED"
            cond_ok = (
                path == COND
                and status == "AWAITING_CONDITION"
                and ctx.get("condition_verified") is True
            )
            assert smart_ok or cond_ok, (path, status, ctx)
        if "VOID" in actions:
            assert path == COND and status == "AWAITING_CONDITION", (path, status, ctx)
        if status in {"CAPTURED", "VOIDED", "CANCELLED"}:
            assert actions == ["NONE"]


def test_allowed_actions_only_offers_provider_actions_that_are_legal_next_moves():
    for path, status, ctx in product(PATHS, ALL_STATUSES - {"UNCERTAIN"}, CONTEXTS):
        actions = allowed_actions(path, status, ctx)
        if "CAPTURE" in actions:
            assert validate_transition(path, status, "CAPTURE_REQUESTED")[0]
        if "VOID" in actions:
            assert validate_transition(path, status, "VOID_REQUESTED")[0]


# -------------------------------------------------------------------- normalization
def test_path_and_status_aliases():
    assert normalize_path("smart") == "SMARTPAY"
    assert normalize_path("Conditional") == "CONDITIONALPAY"
    assert normalize_path("smartpay") == "SMARTPAY"
    assert normalize_path("other") is None
    assert normalize_path(None) is None
    assert normalize_status("created") == "INTENT_CREATED"  # Task 2 table default
    assert normalize_status(" captured ") == "CAPTURED"
    assert normalize_status("nope") is None
    assert normalize_status(5) is None


def test_the_table_default_status_works_with_the_state_machine():
    assert validate_transition(SMART, "CREATED", "ORDER_CREATED") == (True, "")
    assert allowed_actions(COND, "CREATED", {}) == ["WAIT"]


def test_is_final():
    assert is_final("CAPTURED") and is_final("failed")
    assert not is_final("UNCERTAIN") and not is_final("nope")