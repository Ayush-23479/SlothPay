"""Shared contract for condition verification integrations."""

from __future__ import annotations

from typing import Protocol

from backend.extensions.signed_conditions import ConditionEvent


class ConditionVerifier(Protocol):
    """Verify external evidence without moving any funds."""

    def verify(
        self, *, timestamp: str, event_id: str, signature: str, raw_body: bytes
    ) -> ConditionEvent: ...


class SignedWebhookVerifier:
    def __init__(self, secret: str):
        self.secret = secret

    def verify(
        self, *, timestamp: str, event_id: str, signature: str, raw_body: bytes
    ) -> ConditionEvent:
        from backend.extensions.signed_conditions import verify_event

        return verify_event(
            secret=self.secret,
            timestamp=timestamp,
            event_id=event_id,
            signature=signature,
            raw_body=raw_body,
        )
