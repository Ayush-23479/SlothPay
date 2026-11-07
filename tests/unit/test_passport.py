
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from backend.api import routes_passport
from backend.api.deps import install_error_handlers
from backend.api.passport import (
    add_hash_chain,
    build_passport,
    make_token,
    read_token,
    verify_chain,
)
from backend.api.routes_passport import get_passport_source
from backend.api.routes_passport import router as passport_router
from backend.intelligence import fallbacks

SECRET = "s" * 40
OWNER = "owner-secret-id-1"
OTHER = "someone-else"
PAYMENT_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ORDER_ID = "5O190127TN364715T"
CAPTURE_ID = "3C679366HH908993F"
REQUEST_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


def event(seconds, event_type, to_state=None, from_state=None, actor="orchestration", **evidence):
    return {
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state,
        "actor": actor,
        "evidence": evidence,
        "created_at": at(seconds),
    }


def intent(path="SMARTPAY", status="CAPTURED"):
    return {
        "id": str(PAYMENT_ID),
        "path": path,
        "amount": "10.00",
        "currency": "USD",
        "status": status,
        "owner_id": OWNER,
    }


def smartpay_events():
    return [
        event(0, "INTENT_CREATED", "INTENT_CREATED", source="internal_record", confirmed=False),
        event(
            5,
            "STATE_TRANSITION",
            "ORDER_CREATED",
            "INTENT_CREATED",
            source="paypal_response",
            confirmed=True,
            order_id=ORDER_ID,
        ),
        event(
            20,
            "STATE_TRANSITION",
            "CAPTURED",
            "CAPTURE_REQUESTED",
            actor="paypal_webhook",
            source="webhook",
            confirmed=True,
            event_id="WH-1",
            resource_id=CAPTURE_ID,
        ),
    ]


VERIFIED = [{"event_id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "verified": True}]
ATTEMPTS = [
    {"operation": "create_order", "request_id": REQUEST_ID, "status": "SUCCEEDED"},
    {"operation": "capture", "request_id": "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee", "status": "OK"},
]


def passport(**kwargs):
    data = {
        "intent": intent(),
        "events": smartpay_events(),
        "attempts": ATTEMPTS,
        "condition": None,
        "provider_events": VERIFIED,
    }
    data.update(kwargs)
    return build_passport(
        data["intent"],
        data["events"],
        data["attempts"],
        data["condition"],
        data["provider_events"],
        shared=data.get("shared", False),
    )


# ----------------------------------------------------------------------- the builder
def test_a_finished_payment_has_a_timeline_with_confirmed_and_recorded_entries():
    result = passport()
    assert result["summary"]["final_outcome"] == "The payment was completed."
    assert result["summary"]["display_status"] == "COMPLETED"
    flags = [(entry["title"], entry["confirmed"]) for entry in result["timeline"]]
    assert flags == [
        ("Payment started", False),
        ("PayPal order created", True),
        ("Payment completed", True),
    ]
    assert result["integrity"]["chain_valid"] is True


def test_the_summary_masks_the_payment_id():
    result = passport()
    assert result["summary"]["payment_id"] == "1111…5555"
    assert str(PAYMENT_ID) not in json.dumps(result)


@pytest.mark.parametrize(
    "source", ["api", "internal_record", "internal_client", "rescue", "intelligence", None]
)
def test_a_recorded_entry_is_never_shown_as_confirmed(source):
    # Even if the row claims confirmed=True, only PayPal-sourced rows may be confirmed.
    events = [
        event(0, "STATE_TRANSITION", "CAPTURED", "CAPTURE_REQUESTED", source=source, confirmed=True)
    ]
    assert passport(events=events)["timeline"][0]["confirmed"] is False


def test_a_paypal_row_without_the_confirmed_flag_is_recorded():
    events = [event(0, "STATE_TRANSITION", "CAPTURED", source="paypal_get", confirmed=False)]
    assert passport(events=events)["timeline"][0]["confirmed"] is False


def test_a_webhook_row_needs_a_verified_provider_event():
    assert passport(provider_events=[])["timeline"][-1]["confirmed"] is False
    unverified = [{"event_id": "WH-1", "verified": False, "received_at": at(21)}]
    assert passport(provider_events=unverified)["timeline"][-1]["confirmed"] is False
    other = [{"event_id": "WH-OTHER", "verified": True}]
    assert passport(provider_events=other)["timeline"][-1]["confirmed"] is False


def test_a_captured_payment_without_paypal_confirmation_is_described_honestly():
    events = [event(0, "STATE_TRANSITION", "CAPTURED", source="internal_record", confirmed=False)]
    outcome = passport(events=events)["summary"]["final_outcome"]
    assert "not recorded yet" in outcome


def test_a_transition_to_the_same_status_is_not_shown():
    events = [
        event(0, "INTENT_CREATED", source="api", confirmed=False),
        event(
            1,
            "STATE_TRANSITION",
            "INTENT_CREATED",
            "INTENT_CREATED",
            source="internal_record",
            confirmed=False,
        ),
    ]
    timeline = passport(events=events)["timeline"]
    assert [entry["title"] for entry in timeline] == ["Payment started"]
    assert "to “Payment started”" not in json.dumps(timeline)


def test_hidden_events_are_not_in_the_timeline():
    events = smartpay_events() + [
        event(30, "AI_EXPLANATION", source="intelligence", confirmed=False),
        event(31, "RESCUE_PLAN_BUILT", source="rescue", confirmed=False),
        event(32, "OPERATION_ATTEMPT_STARTED", source="internal_client", confirmed=False),
    ]
    assert len(passport(events=events)["timeline"]) == 3


# ----------------------------------------------------------------------- hash chain
def test_the_hash_chain_links_each_entry_to_the_one_before():
    timeline = passport()["timeline"]
    assert len({entry["entry_hash"] for entry in timeline}) == len(timeline)
    assert verify_chain(timeline) is True


@pytest.mark.parametrize("field, value", [("title", "Edited"), ("confirmed", False), ("time", "x")])
def test_the_hash_chain_breaks_when_an_entry_is_changed(field, value):
    timeline = copy.deepcopy(passport()["timeline"])
    timeline[1][field] = value if timeline[1][field] != value else "other"
    assert verify_chain(timeline) is False


def test_the_hash_chain_breaks_when_an_entry_is_removed_or_reordered():
    timeline = passport()["timeline"]
    assert verify_chain(timeline[1:]) is False
    assert verify_chain([timeline[1], timeline[0], timeline[2]]) is False
    assert verify_chain([{**timeline[0], "entry_hash": "0" * 64}]) is False


def test_the_chain_is_deterministic():
    entries = [{"time": "t", "title": "a"}, {"time": "u", "title": "b"}]
    assert add_hash_chain(entries) == add_hash_chain(entries)
    assert verify_chain(add_hash_chain(entries))
    assert verify_chain([]) is True


# ------------------------------------------------------------------------- condition
def test_a_smartpay_passport_has_no_condition_block():
    assert passport()["condition"] is None


def test_a_conditionalpay_passport_shows_the_condition():
    condition = {
        "description": "Parcel arrives",
        "status": "VERIFIED",
        "verified_at": at(60),
        "evidence": {"source": "dashboard_demo"},
    }
    result = passport(intent=intent("CONDITIONALPAY", "CAPTURED"), condition=condition)
    block = result["condition"]
    assert block["required"] == "Parcel arrives"
    assert block["status"] == "VERIFIED"
    assert block["verified_at"] == at(60).isoformat()
    assert "met" in block["evidence_summary"]


def test_a_pending_condition_says_there_is_no_evidence_yet():
    result = passport(intent=intent("CONDITIONALPAY", "AWAITING_CONDITION"), condition=None)
    assert result["condition"]["status"] == "PENDING"
    assert "No evidence" in result["condition"]["evidence_summary"]


def test_raw_condition_evidence_is_not_copied_through():
    condition = {
        "description": "x",
        "status": "VERIFIED",
        "evidence": {"source": "ok", "note": "alice@example.com", "token": "SECRET-TOKEN"},
    }
    text = json.dumps(passport(intent=intent("CONDITIONALPAY"), condition=condition))
    assert "alice@example.com" not in text
    assert "SECRET-TOKEN" not in text


# --------------------------------------------------------------------------- errors
def lost_answer_events():
    return [
        event(0, "INTENT_CREATED", "INTENT_CREATED", source="internal_record", confirmed=False),
        event(
            5,
            "PROVIDER_OPERATION_OUTCOME_UNKNOWN",
            actor="orchestration",
            source="internal_client",
            confirmed=False,
            operation="capture",
            request_id=REQUEST_ID,
        ),
        event(
            6,
            "STATE_TRANSITION",
            "UNCERTAIN",
            "CAPTURE_REQUESTED",
            source="internal_client",
            confirmed=False,
        ),
        event(15, "RECONCILED", actor="reconciliation", source="reconcile", confirmed=True),
        event(
            16,
            "STATE_TRANSITION",
            "CAPTURED",
            "UNCERTAIN",
            actor="reconciliation",
            source="reconcile",
            confirmed=True,
        ),
    ]


def test_a_lost_answer_becomes_an_error_with_a_plain_explanation():
    result = passport(events=lost_answer_events(), provider_events=[])
    [error] = result["errors"]
    assert error["error_class"] == "RETRY"
    assert error["operation"] == "capture"
    assert error["plain_explanation"] == fallbacks.ERROR_TEXT["RETRY"][0]
    assert error["explanation_source"] == "template"
    assert "did not send the request again" in error["what_slothpay_did"].lower()
    assert "settled" in error["what_slothpay_did"]
    assert result["protections"]["reconciled"] is True


def test_the_ai_explanation_text_is_used_when_it_was_recorded():
    events = lost_answer_events() + [
        event(
            7,
            "AI_EXPLANATION",
            actor="intelligence",
            source="intelligence",
            confirmed=False,
            details={"source": "model", "action": "explained_RETRY"},
            text={"message": "PayPal did not reply in time.", "next_step": "Just wait a moment."},
        )
    ]
    [error] = passport(events=events, provider_events=[])["errors"]
    assert error["plain_explanation"] == "PayPal did not reply in time."
    assert error["next_step"] == "Just wait a moment."
    assert error["explanation_source"] == "model"


def test_an_ai_text_with_unsafe_characters_is_ignored():
    events = lost_answer_events() + [
        event(
            7,
            "AI_EXPLANATION",
            details={"source": "model", "action": "explained_RETRY"},
            text={"message": "<script>alert(1)</script>", "next_step": "ok"},
        )
    ]
    [error] = passport(events=events, provider_events=[])["errors"]
    assert error["plain_explanation"] == fallbacks.ERROR_TEXT["RETRY"][0]


def test_a_failed_payment_lists_its_error_class_text():
    events = [
        event(0, "INTENT_CREATED", "INTENT_CREATED", source="internal_record", confirmed=False),
        event(
            9,
            "STATE_TRANSITION",
            "FAILED",
            "CAPTURE_REQUESTED",
            source="paypal_response",
            confirmed=True,
            error_class="SWITCH_METHOD",
            operation="capture",
        ),
    ]
    result = passport(intent=intent(status="FAILED"), events=events, provider_events=[])
    [error] = result["errors"]
    assert error["error_class"] == "SWITCH_METHOD"
    assert error["plain_explanation"] == fallbacks.ERROR_TEXT["SWITCH_METHOD"][0]
    assert result["summary"]["final_outcome"].startswith("The payment did not go through")


def test_an_unknown_error_class_falls_back_to_a_person_must_look():
    events = [
        event(9, "STATE_TRANSITION", "FAILED", error_class="MADE_UP", source="paypal_response")
    ]
    [error] = passport(intent=intent(status="FAILED"), events=events, provider_events=[])["errors"]
    assert error["error_class"] == "ESCALATE"


def test_an_unverified_webhook_is_listed_as_an_error_that_changed_nothing():
    rejected = [{"event_id": "WH-X", "verified": False, "received_at": at(3)}]
    [error] = passport(provider_events=VERIFIED + rejected)["errors"]
    assert error["error_class"] == "WEBHOOK_UNVERIFIED"
    assert "changed nothing" in error["what_slothpay_did"]


def test_a_clean_payment_has_no_errors():
    assert passport()["errors"] == []


# ----------------------------------------------------------------------- protections
def test_protections_count_duplicates_and_report_zero_blind_retries():
    events = smartpay_events() + [
        event(40, "DUPLICATE_SUPPRESSED", actor="api", source="api", confirmed=False),
        event(41, "DUPLICATE_SUPPRESSED", actor="api", source="api", confirmed=False),
    ]
    result = passport(events=events)["protections"]
    assert result["duplicate_replays"] == 2
    assert result["blind_retries"] == 0
    assert result["safe_retries"] == 0
    assert result["reconciled"] is False
    assert len(result["paypal_request_ids_used"]) == 2


def test_the_request_ids_are_masked():
    ids = passport()["protections"]["paypal_request_ids_used"]
    assert ids[0] == "aaaa…eeee"
    assert REQUEST_ID not in json.dumps(passport())


def test_a_new_request_id_for_the_same_operation_counts_as_a_blind_retry():
    attempts = ATTEMPTS + [
        {
            "operation": "capture",
            "request_id": "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "status": "X",
        }
    ]
    assert passport(attempts=attempts)["protections"]["blind_retries"] == 1


def test_a_safe_retry_is_counted_separately():
    events = smartpay_events() + [
        event(50, "RESCUE_RETRY_CLAIMED", source="rescue", confirmed=False)
    ]
    protections = passport(events=events)["protections"]
    assert protections["safe_retries"] == 1
    assert protections["blind_retries"] == 0


# ------------------------------------------------------------------------ shared view
def leaky_events():
    return smartpay_events() + [
        event(
            60,
            "RESCUE_ACTION_EXECUTED",
            actor=f"owner:{OWNER}",
            source="rescue",
            confirmed=False,
            note=f"by {OWNER}",
        ),
        event(61, "SOMETHING_ELSE", actor=f"owner:{OWNER}", confirmed=False),
    ]


def test_the_shared_view_has_no_owner_id_and_no_paypal_ids():
    result = passport(events=leaky_events(), shared=True)
    text = json.dumps(result)
    assert result["view"] == "shared"
    for secret in (OWNER, ORDER_ID, CAPTURE_ID, str(PAYMENT_ID), REQUEST_ID):
        assert secret not in text


def test_the_owner_view_does_not_expose_owner_or_paypal_ids_either():
    text = json.dumps(passport(events=leaky_events()))
    for secret in (OWNER, ORDER_ID, CAPTURE_ID):
        assert secret not in text


def test_the_shared_view_scrubs_a_stray_owner_id_and_keeps_the_chain_valid():
    events = smartpay_events() + [event(70, f"CUSTOM_{OWNER}", confirmed=False)]
    result = passport(events=events, shared=True)
    assert OWNER not in json.dumps(result)
    assert result["integrity"]["chain_valid"] is True


# ------------------------------------------------------------------------- the token
def test_a_token_round_trips():
    token, expires = make_token(PAYMENT_ID, SECRET, NOW)
    assert expires == NOW + timedelta(days=7)
    assert read_token(token, SECRET, NOW + timedelta(days=6)) == PAYMENT_ID


def test_an_expired_token_is_refused():
    token, _ = make_token(PAYMENT_ID, SECRET, NOW)
    assert read_token(token, SECRET, NOW + timedelta(days=7, seconds=1)) is None
    assert read_token(token, SECRET, NOW + timedelta(days=7)) is None


def test_a_token_signed_with_another_secret_is_refused():
    token, _ = make_token(PAYMENT_ID, SECRET, NOW)
    assert read_token(token, "other-secret-" + "x" * 30, NOW) is None


def test_a_tampered_token_is_refused():
    token, _ = make_token(PAYMENT_ID, SECRET, NOW)
    payload, signature = token.split(".")
    other, _ = make_token(uuid.uuid4(), SECRET, NOW)
    assert read_token(other.split(".")[0] + "." + signature, SECRET, NOW) is None
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    assert read_token(f"{payload}.{flipped}", SECRET, NOW) is None
    assert read_token(f"{payload}x.{signature}", SECRET, NOW) is None


@pytest.mark.parametrize(
    "bad", ["", ".", "abc", "a.b.c", "....", "not-base64!.sig", "x" * 500, "é.é"]
)
def test_garbage_tokens_are_refused(bad):
    assert read_token(bad, SECRET, NOW) is None


def test_a_token_cannot_be_made_or_read_without_a_secret():
    with pytest.raises(ValueError):
        make_token(PAYMENT_ID, "", NOW)
    token, _ = make_token(PAYMENT_ID, SECRET, NOW)
    assert read_token(token, "", NOW) is None


# ------------------------------------------------------------------------- the routes
class FakeSource:
    def __init__(self, data=None):
        self.data = data

    async def load(self, payment_id):
        if self.data is None or str(payment_id) != self.data["intent"]["id"]:
            return None
        return self.data


def good_data(**kwargs):
    data = {
        "intent": intent(),
        "events": leaky_events(),
        "attempts": ATTEMPTS,
        "condition": None,
        "provider_events": VERIFIED,
    }
    data.update(kwargs)
    return data


@pytest.fixture
def secret(monkeypatch):
    from backend.config.settings import get_settings

    monkeypatch.setenv("PASSPORT_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:3000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def client(data=None):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(passport_router)
    app.dependency_overrides[get_passport_source] = lambda: FakeSource(data)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def header(owner=OWNER):
    return {"X-Owner-Id": owner}


async def test_the_owner_gets_the_passport(secret):
    async with client(good_data()) as http:
        response = await http.get(f"/api/payments/{PAYMENT_ID}/passport", headers=header())
    body = response.json()
    assert response.status_code == 200
    assert body["view"] == "owner"
    assert body["integrity"]["chain_valid"] is True


async def test_another_owner_and_a_missing_payment_look_identical(secret):
    async with client(good_data()) as http:
        other = await http.get(f"/api/payments/{PAYMENT_ID}/passport", headers=header(OTHER))
        missing = await http.get(f"/api/payments/{uuid.uuid4()}/passport", headers=header())
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()


async def test_the_owner_passport_needs_an_owner(secret):
    async with client(good_data()) as http:
        response = await http.get(f"/api/payments/{PAYMENT_ID}/passport")
    assert response.status_code == 401


async def test_sharing_gives_a_link_that_shows_the_shared_view(secret):
    async with client(good_data()) as http:
        shared = await http.post(f"/api/payments/{PAYMENT_ID}/passport/share", headers=header())
        assert shared.status_code == 200
        link = shared.json()
        assert link["url"] == f"http://localhost:3000/passport/{link['token']}"
        # no owner header: the link alone is enough
        response = await http.get(f"/api/passport/{link['token']}")
    body = response.json()
    assert response.status_code == 200
    assert body["view"] == "shared"
    text = json.dumps(body)
    for hidden in (OWNER, ORDER_ID, CAPTURE_ID, str(PAYMENT_ID)):
        assert hidden not in text


async def test_only_the_owner_can_make_a_share_link(secret):
    async with client(good_data()) as http:
        other = await http.post(f"/api/payments/{PAYMENT_ID}/passport/share", headers=header(OTHER))
        anonymous = await http.post(f"/api/payments/{PAYMENT_ID}/passport/share")
    assert other.status_code == 404
    assert anonymous.status_code == 401


async def test_a_tampered_token_returns_404(secret):
    token, _ = make_token(PAYMENT_ID, SECRET, datetime.now(UTC))
    payload, signature = token.split(".")
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    async with client(good_data()) as http:
        tampered = await http.get(f"/api/passport/{payload}.{flipped}")
        junk = await http.get("/api/passport/not-a-token")
    assert tampered.status_code == 404
    assert junk.status_code == 404
    assert tampered.json()["error"] == "not_found"


async def test_an_expired_token_returns_404(secret):
    token, _ = make_token(PAYMENT_ID, SECRET, datetime.now(UTC) - timedelta(days=8))
    async with client(good_data()) as http:
        response = await http.get(f"/api/passport/{token}")
    assert response.status_code == 404


async def test_a_valid_token_for_a_missing_payment_returns_404(secret):
    token, _ = make_token(uuid.uuid4(), SECRET, datetime.now(UTC))
    async with client(good_data()) as http:
        response = await http.get(f"/api/passport/{token}")
    assert response.status_code == 404


async def test_sharing_without_a_signing_secret_is_unavailable(monkeypatch):
    from backend.config.settings import get_settings

    monkeypatch.setenv("PASSPORT_SIGNING_SECRET", "")
    get_settings.cache_clear()
    try:
        async with client(good_data()) as http:
            response = await http.post(
                f"/api/payments/{PAYMENT_ID}/passport/share", headers=header()
            )
            link = await http.get("/api/passport/anything.at-all")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 503
    assert link.status_code == 404


async def test_a_smartpay_passport_over_the_route_has_no_condition(secret):
    async with client(good_data()) as http:
        response = await http.get(f"/api/payments/{PAYMENT_ID}/passport", headers=header())
    assert response.json()["condition"] is None


def test_the_routes_module_exposes_the_fixed_404_text():
    assert routes_passport.passport_not_found().status_code == 404