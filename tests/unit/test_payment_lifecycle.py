"""Offline Task 6 lifecycle mapping and idempotency-contract tests."""

import uuid

import pytest

from backend.orchestration.lifecycle import (
    LifecycleSafetyError,
    _provider_result,
    _request_id,
    stable_operation_request_id,
)


def test_stable_request_id_for_same_operation():
    payment_id = uuid.uuid4()
    first = stable_operation_request_id(payment_id, "capture")
    assert first == stable_operation_request_id(payment_id, "capture")
    assert first != stable_operation_request_id(payment_id, "void")
    assert len(first) <= 128


def test_request_id_cannot_be_overridden():
    with pytest.raises(LifecycleSafetyError):
        _request_id(uuid.uuid4(), "capture", "new-id")


def test_unknown_operation_rejected():
    with pytest.raises(LifecycleSafetyError):
        stable_operation_request_id(uuid.uuid4(), "refund")


def test_smartpay_nested_capture_completed():
    response = {
        "purchase_units": [{"payments": {"captures": [{"id": "CAPTURE_1", "status": "COMPLETED"}]}}]
    }
    assert _provider_result("capture", response, "SMARTPAY") == ("CAPTURED", "CAPTURE_1")


def test_smartpay_nested_capture_pending():
    response = {
        "purchase_units": [{"payments": {"captures": [{"id": "CAPTURE_1", "status": "PENDING"}]}}]
    }
    assert _provider_result("capture", response, "SMARTPAY") == ("CAPTURE_PENDING", "CAPTURE_1")


def test_smartpay_top_level_completed_not_proof():
    with pytest.raises(LifecycleSafetyError):
        _provider_result("capture", {"id": "ORDER_1", "status": "COMPLETED"}, "SMARTPAY")


def test_conditional_authorize_requires_nested_authorization():
    response = {
        "purchase_units": [
            {"payments": {"authorizations": [{"id": "AUTH_1", "status": "CREATED"}]}}
        ]
    }
    assert _provider_result("authorize", response, "CONDITIONALPAY") == ("AUTHORIZED", "AUTH_1")
    with pytest.raises(LifecycleSafetyError):
        _provider_result("authorize", {"status": "COMPLETED"}, "CONDITIONALPAY")


def test_conditional_direct_capture():
    response = {"id": "CAP_2", "status": "COMPLETED"}
    assert _provider_result("capture", response, "CONDITIONALPAY") == ("CAPTURED", "CAP_2")


def test_void_requires_204():
    assert _provider_result("void", {"http_status": 204}, "CONDITIONALPAY") == ("VOIDED", None)
    with pytest.raises(LifecycleSafetyError):
        _provider_result("void", {"status": "VOIDED"}, "CONDITIONALPAY")


def test_multiple_captures_fail_closed():
    response = {
        "purchase_units": [
            {
                "payments": {
                    "captures": [
                        {"id": "A", "status": "COMPLETED"},
                        {"id": "B", "status": "COMPLETED"},
                    ]
                }
            }
        ]
    }
    with pytest.raises(LifecycleSafetyError):
        _provider_result("capture", response, "SMARTPAY")
