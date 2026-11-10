import json
import uuid

import pytest

from backend.extensions.signed_conditions import (
    InvalidConditionEvent,
    sign_event,
    verify_event,
)

SECRET = "test-secret-for-task-18"
NOW = 1800000000
PAYMENT_ID = str(uuid.uuid4())


def make_event(verified=True):
    body = json.dumps(
        {"payment_id": PAYMENT_ID, "verified": verified}
    ).encode()

    args = {
        "secret": SECRET,
        "timestamp": str(NOW),
        "event_id": "delivery-event-001",
        "raw_body": body,
        "now": NOW,
    }
    args["signature"] = sign_event(
        SECRET,
        NOW,
        args["event_id"],
        body,
    )
    return args


@pytest.mark.parametrize("verified", [True, False])
def test_valid_signed_condition(verified):
    event = verify_event(**make_event(verified))
    assert event.verified is verified
    assert str(event.payment_id) == PAYMENT_ID


def test_wrong_signature_rejected():
    args = make_event()
    args["signature"] = "0" * 64
    with pytest.raises(InvalidConditionEvent):
        verify_event(**args)


def test_modified_body_rejected():
    args = make_event(True)
    args["raw_body"] = json.dumps({
        "payment_id": PAYMENT_ID,
        "verified": False,
    }).encode()
    with pytest.raises(InvalidConditionEvent):
        verify_event(**args)


@pytest.mark.parametrize("offset", [-301, 301])
def test_stale_or_future_timestamp_rejected(offset):
    args = make_event()
    args["now"] = NOW + offset
    with pytest.raises(InvalidConditionEvent):
        verify_event(**args)


def test_invalid_boolean_rejected():
    args = make_event()
    args["raw_body"] = json.dumps({
        "payment_id": PAYMENT_ID,
        "verified": "true",
    }).encode()
    args["signature"] = sign_event(
        SECRET, NOW, args["event_id"], args["raw_body"]
    )
    with pytest.raises(InvalidConditionEvent):
        verify_event(**args)


def test_empty_secret_rejected():
    args = make_event()
    args["secret"] = ""
    with pytest.raises(InvalidConditionEvent):
        verify_event(**args)


def test_non_ascii_signature_rejected():
    args = make_event()
    args["signature"] = "é" * 64
    with pytest.raises(InvalidConditionEvent):
        verify_event(**args)
