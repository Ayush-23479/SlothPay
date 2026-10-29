"""No network or money calls: fail-closed Task 6 evidence classification."""

import pytest

from backend.orchestration.reconcile import classify_provider_observation


def observation(operation, order=None, mode="SMARTPAY", authorization=None):
    return classify_provider_observation(
        operation=operation,
        payment_mode=mode,
        order=order,
        authorization=authorization,
    )


def test_order_completed_without_capture_is_not_capture_proof():
    result = observation("capture", {"id": "ORDER1", "status": "COMPLETED"})
    assert result[2] == "MANUAL_REVIEW"


def test_one_completed_capture_is_positive_evidence():
    order = {
        "id": "ORDER1",
        "purchase_units": [{"payments": {"captures": [{"id": "CAP1", "status": "COMPLETED"}]}}],
    }
    assert observation("capture", order)[:3] == ("COMPLETED", "CAP1", "EVIDENCE_FOUND")


def test_multiple_captures_do_not_select_arbitrarily():
    order = {
        "purchase_units": [
            {
                "payments": {
                    "captures": [
                        {"id": "CAP1", "status": "COMPLETED"},
                        {"id": "CAP2", "status": "COMPLETED"},
                    ]
                }
            }
        ],
    }
    assert observation("capture", order)[2] == "MANUAL_REVIEW"


def test_authorization_requires_embedded_id():
    order = {
        "purchase_units": [{"payments": {"authorizations": [{"status": "CREATED"}]}}],
    }
    assert observation("authorize", order, "CONDITIONALPAY")[2] == "MANUAL_REVIEW"


def test_void_requires_verified_authorization_identifier():
    assert observation(
        "void",
        mode="CONDITIONALPAY",
        authorization={"id": "AUTH1", "status": "VOIDED"},
    )[:3] == ("VOIDED", "AUTH1", "EVIDENCE_FOUND")


@pytest.mark.parametrize("operation", ["create_order", "authorize", "capture", "void"])
def test_no_provider_evidence_never_claims_success(operation):
    assert observation(operation)[2] == "MANUAL_REVIEW"
