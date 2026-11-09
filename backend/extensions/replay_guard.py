"""Fail-closed Redis replay guard for signed condition events."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from redis.asyncio import Redis

PENDING_TTL = 300
PROCESSED_TTL = 7 * 24 * 60 * 60


class ReplayRejected(Exception):
    """An event has already been received or processed."""


@dataclass
class ReplayGuard:
    redis: Redis

    @staticmethod
    def _key(event_id: str) -> str:
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        return f"slothpay:task18:condition-event:{digest}"

    async def claim(self, event_id: str) -> None:
        """Atomically reserve an event before processing it."""
        key = self._key(event_id)

        accepted = await self.redis.set(
            key,
            "processing",
            nx=True,
            ex=PENDING_TTL,
        )

        if not accepted:
            raise ReplayRejected("Event already received")

    async def complete(self, event_id: str) -> None:
        """Retain the replay marker after successful processing."""
        key = self._key(event_id)

        # Complete only an existing reservation. Never create one here.
        updated = await self.redis.set(
            key,
            "processed",
            xx=True,
            ex=PROCESSED_TTL,
        )
        if not updated:
            raise ReplayRejected("Event reservation expired")
