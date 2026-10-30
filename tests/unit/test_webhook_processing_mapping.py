"""Offline webhook evidence safety checks."""

from backend.webhooks.processing import extract_capture_id


def candidate(kind="PAYMENT.CAPTURE.COMPLETED", status="COMPLETED", cid="C1"):
    return {"event_type": kind, "resource": {"id": cid, "status": status}}


def test_missing_fields_rejected():
    assert extract_capture_id({"event_type": "PAYMENT.CAPTURE.COMPLETED"}) is None
    assert (
        extract_capture_id({"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "C1"}})
        is None
    )


def test_only_completed_capture_is_candidate():
    assert extract_capture_id(candidate(kind="PAYMENT.CAPTURE.PENDING")) is None
    assert extract_capture_id(candidate(status="PENDING")) is None


def test_completed_capture_id():
    assert extract_capture_id(candidate()) == "C1"
