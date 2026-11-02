from __future__ import annotations

from contextvars import ContextVar
from typing import Protocol, runtime_checkable


class TransportLost(Exception):
    """The connection to PayPal was lost (really, or on purpose by the chaos controller)."""


@runtime_checkable
class FailureInjector(Protocol):
    async def before_send(self, operation: str) -> dict[str, str]:
        """May sleep, raise TransportLost (request never leaves), or return extra headers."""
        ...

    async def after_send(self, operation: str) -> None:
        """May raise TransportLost to lose the answer AFTER PayPal received the request."""
        ...


class NoopInjector:
    async def before_send(self, operation: str) -> dict[str, str]:
        return {}

    async def after_send(self, operation: str) -> None:
        return None


NOOP = NoopInjector()

# The chaos mode that actually fired during the current call. The lifecycle reads it once
# the call ends and stores it on the attempt row (payment_attempts.chaos_mode).
_fired_mode: ContextVar[str | None] = ContextVar("slothpay_chaos_fired_mode", default=None)

_override: FailureInjector | None = None


def note_chaos_mode(mode: str) -> None:
    _fired_mode.set(mode)


def take_chaos_mode() -> str | None:
    """Return the chaos mode that fired during this call, and forget it."""
    mode = _fired_mode.get()
    _fired_mode.set(None)
    return mode


def set_default_injector(injector: FailureInjector | None) -> None:
    """Replace the injector used by clients built without one (tests; None restores default)."""
    global _override
    _override = injector


def default_injector() -> FailureInjector:
    """What a PayPalClient built without an injector uses.

    Chaos only when the environment allows it (local or sandbox, CHAOS_ENABLED true);
    otherwise a no-op. Every process (API, workers) decides the same way from its settings,
    and the live mode itself is read from Redis, so all of them agree.
    """
    if _override is not None:
        return _override
    from backend.config.settings import get_settings
    from backend.reliability.chaos import ChaosController, chaos_allowed

    settings = get_settings()
    if not chaos_allowed(settings):
        return NOOP
    return ChaosController(settings)


__all__ = [
    "NOOP",
    "FailureInjector",
    "NoopInjector",
    "TransportLost",
    "default_injector",
    "note_chaos_mode",
    "set_default_injector",
    "take_chaos_mode",
]