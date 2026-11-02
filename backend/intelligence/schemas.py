from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_FLAGS = 5


def _clean(value: Any) -> Any:
    return _CONTROL.sub(" ", value).strip() if isinstance(value, str) else value


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _strip_controls(cls, value: Any) -> Any:
        return _clean(value)


Text120 = Annotated[str, Field(min_length=1, max_length=120)]
Text300 = Annotated[str, Field(min_length=1, max_length=300)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class PaymentAnalysis(_Model):
    summary: Annotated[str, Field(min_length=1, max_length=400)]
    risk_flags: list[Text120] = Field(default_factory=list)
    confidence: Confidence

    @field_validator("risk_flags", mode="before")
    @classmethod
    def _at_most_five(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [_clean(item) for item in value if item][:MAX_FLAGS]
        return value


class ErrorExplanation(_Model):
    message: Text300
    next_step: Text300


class LineItem(_Model):
    description: Annotated[str, Field(min_length=1, max_length=200)]
    amount: Decimal


class InvoiceExtraction(_Model):
    payee_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    invoice_number: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    amount: Decimal
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    due_date: date | None = None
    line_items: list[LineItem] = Field(default_factory=list, max_length=50)
    confidence: Confidence
    warnings: list[Text300] = Field(default_factory=list, max_length=10)


class RecoveryChoice(_Model):
    recommended_action: Annotated[str, Field(min_length=1, max_length=40)]
    reason: Text300