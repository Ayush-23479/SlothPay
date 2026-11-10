from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    func,
    insert,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

timings_metadata = MetaData()

payment_timings = Table(
    "payment_timings",
    timings_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("intent_id", UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False),
    Column("step", String(48), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("ms", Numeric(12, 3), nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_payment_timings_intent", "intent_id"),
    Index("ix_payment_timings_step_ts", "step", "ts"),
)


async def insert_timings(session: AsyncSession, rows: Iterable[dict[str, Any]]) -> None:
    values = [
        {
            "intent_id": uuid.UUID(str(row["intent_id"])),
            "step": str(row["step"])[:48],
            "kind": row["kind"],
            "ms": row["ms"],
        }
        for row in rows
    ]
    if values:
        await session.execute(insert(payment_timings), values)