from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DisplayStatus = Literal["COMPLETED", "PENDING", "ACTION_NEEDED", "CLOSED"]
PathName = Literal["SMARTPAY", "CONDITIONALPAY"]
RescueActionName = Literal[
    "WAIT",
    "RECONCILE",
    "RETRY_SAME_REQUEST",
    "CAPTURE",
    "VOID",
    "ASK_BUYER_TO_APPROVE",
    "ASK_BUYER_NEW_METHOD",
    "CONTACT_SUPPORT",
]

MAX_CONDITION_MINUTES = 48 * 60  # the same ceiling as CONDITION_MAX_HOURS (Task 9)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ----------------------------------------------------------------------------- requests
class ConditionIn(_Strict):
    """What must happen before held funds are captured (ConditionalPay only)."""

    kind: Annotated[str, Field(min_length=1, max_length=64)]
    description: Annotated[str, Field(min_length=1, max_length=200)]
    deadline_minutes: Annotated[int, Field(ge=1, le=MAX_CONDITION_MINUTES, strict=True)]


class PaymentCreate(_Strict):
    path: PathName
    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    description: Annotated[str | None, Field(max_length=200)] = None
    condition: ConditionIn | None = None
    # Optional: says which purchase this is. The same id is the same purchase.
    merchant_order_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None

    @model_validator(mode="after")
    def _condition_matches_path(self) -> PaymentCreate:
        if self.path == "CONDITIONALPAY" and self.condition is None:
            raise ValueError("A ConditionalPay payment needs a condition")
        if self.path == "SMARTPAY" and self.condition is not None:
            raise ValueError("A SmartPay payment cannot have a condition")
        return self

    def to_payload(self) -> dict[str, Any]:
        """The dict the duplicate-protection layer (Task 5) expects."""
        payload: dict[str, Any] = {
            "path": self.path,
            "amount": str(self.amount),
            "currency": self.currency.upper(),
            "description": self.description,
            "condition": self.condition.model_dump() if self.condition else None,
        }
        if self.merchant_order_id is not None:
            payload["merchant_order_id"] = self.merchant_order_id
        return payload


class ConditionVerifyIn(_Strict):
    """The seller's answer. Only a real JSON `true` can ever release the funds."""

    verified: bool = Field(strict=True)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _small_evidence(self) -> ConditionVerifyIn:
        if len(self.evidence) > 20:
            raise ValueError("evidence has too many fields")
        return self


class RescueActionIn(_Strict):
    action: RescueActionName


# ---------------------------------------------------------------------------- responses
class PayPalIdsOut(BaseModel):
    """Masked PayPal identifiers (first 4 and last 4 characters only)."""

    order_id: str | None = None
    authorization_id: str | None = None
    capture_id: str | None = None


class PaymentOut(BaseModel):
    id: str
    path: PathName
    amount: Annotated[str, Field(pattern=r"^\d+\.\d{2}$")]
    currency: str
    status: str
    display_status: DisplayStatus
    allowed_actions: list[str]
    approval_url: str | None = None
    paypal_ids: PayPalIdsOut = Field(default_factory=PayPalIdsOut)
    created_at: datetime
    updated_at: datetime


class PaymentListOut(BaseModel):
    items: list[PaymentOut]
    next_cursor: str | None = None


class EventOut(BaseModel):
    id: str
    type: str
    title: str
    summary: str
    from_status: str | None = None
    to_status: str | None = None
    source: str
    confirmed: bool
    created_at: datetime


class RescueOut(BaseModel):
    payment_id: str
    display_status: DisplayStatus
    facts: list[str]
    allowed_actions: list[str]
    recommended_action: str
    needs_human: bool


class RescueActionOut(BaseModel):
    action: str
    result: dict[str, Any]


class MetricsOut(BaseModel):
    """Shape reserved for the monitoring task (Task 12). No route serves it yet."""

    total_payments: int
    by_display_status: dict[str, int]
    reconciliations_open: int = 0
    generated_at: datetime


class ErrorOut(BaseModel):
    """Every error from /api/* has this shape (validation errors keep FastAPI's 422 body)."""

    error: str
    message: str
    retry_after: int | None = None