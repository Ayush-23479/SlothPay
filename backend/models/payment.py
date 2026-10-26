"""Authoritative *internal* payment intent (not a PayPal authorization)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint("char_length(currency) = 3", name="ck_payments_currency_length"),
        CheckConstraint("mode IN ('SMARTPAY', 'CONDITIONALPAY')", name="ck_payments_mode"),
        UniqueConstraint("merchant_id", "merchant_order_id", name="uq_payments_order"),
        UniqueConstraint("merchant_id", "intent_key", name="uq_payments_intent_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    merchant_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    intent_key: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False, default="CREATED")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paypal_order_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
