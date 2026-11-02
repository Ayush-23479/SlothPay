from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any

from backend.config.settings import Settings
from backend.payments.hooks import TransportLost, note_chaos_mode

log = logging.getLogger("slothpay.chaos")

MODES = ("none", "delay", "drop_before_send", "drop_after_send", "mock_error")
CHAOS_ENVS = frozenset({"local", "sandbox"})
STATE_KEY = "slothpay:chaos:state"
REMAINING_KEY = "slothpay:chaos:remaining"
MAX_DELAY_MS = 10_000
MAX_REMAINING = 1_000
MOCK_HEADER = "PayPal-Mock-Response"
_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")

# Decided in before_send, acted on in after_send (same task, same call).
_lose_answer: ContextVar[bool] = ContextVar("slothpay_chaos_lose_answer", default=False)


class ChaosDisabled(Exception):
    """Chaos is not allowed in this environment or is switched off."""


class ChaosInvalid(ValueError):
    """The requested chaos settings are not valid."""


def chaos_allowed(settings: Settings) -> bool:
    return bool(settings.CHAOS_ENABLED) and settings.ENV in CHAOS_ENVS


@dataclass(frozen=True)
class ChaosState:
    mode: str = "none"
    probability: float = 1.0
    delay_ms: int = 0
    code: str | None = None
    remaining_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


OFF = ChaosState()


def validate_state(
    mode: str,
    probability: float = 1.0,
    delay_ms: int = 0,
    code: str | None = None,
    remaining_count: int | None = None,
) -> ChaosState:
    if mode not in MODES:
        raise ChaosInvalid(f"mode must be one of {', '.join(MODES)}")
    if isinstance(probability, bool) or not isinstance(probability, int | float):
        raise ChaosInvalid("probability must be a number between 0 and 1")
    if not 0.0 <= float(probability) <= 1.0:
        raise ChaosInvalid("probability must be between 0 and 1")
    if (
        isinstance(delay_ms, bool)
        or not isinstance(delay_ms, int)
        or not 0 <= delay_ms <= MAX_DELAY_MS
    ):
        raise ChaosInvalid(f"delay_ms must be a whole number from 0 to {MAX_DELAY_MS}")
    if remaining_count is not None and (
        isinstance(remaining_count, bool)
        or not isinstance(remaining_count, int)
        or not 1 <= remaining_count <= MAX_REMAINING
    ):
        raise ChaosInvalid(f"remaining_count must be a whole number from 1 to {MAX_REMAINING}")
    if mode == "mock_error":
        if code is None or not _CODE.fullmatch(code):
            raise ChaosInvalid("mock_error needs a code such as INSTRUMENT_DECLINED")
    elif code is not None and not _CODE.fullmatch(code):
        raise ChaosInvalid("code must look like INSTRUMENT_DECLINED")
    if mode == "none":
        return OFF
    return ChaosState(
        mode=mode,
        probability=float(probability),
        delay_ms=int(delay_ms),
        code=code,
        remaining_count=remaining_count,
    )


def _parse_state(raw: Any) -> ChaosState:
    """Read a stored state; anything odd means no chaos."""
    try:
        data = json.loads(raw)
        return validate_state(
            data.get("mode", "none"),
            data.get("probability", 1.0),
            data.get("delay_ms", 0),
            data.get("code"),
            data.get("remaining_count"),
        )
    except (TypeError, ValueError, AttributeError):
        return OFF


class ChaosController:
    """Implements FailureInjector. Safe to build per call: it holds no state of its own."""

    def __init__(
        self,
        settings: Settings,
        redis: Any | None = None,
        *,
        rng: Callable[[], float] = random.random,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._rng = rng
        self._sleep = sleep

    @property
    def allowed(self) -> bool:
        return chaos_allowed(self._settings)

    def _client(self) -> Any:
        if self._redis is None:
            from backend.notifications.hub import get_redis

            self._redis = get_redis()
        return self._redis

    # ------------------------------------------------------------------ live settings
    async def get_state(self) -> ChaosState:
        if not self.allowed:
            return OFF
        try:
            raw = await self._client().get(STATE_KEY)
        except Exception as exc:  # Redis down: no chaos, never an error for a payment
            log.warning("chaos state unavailable: %s", type(exc).__name__)
            return OFF
        return OFF if raw is None else _parse_state(raw)

    async def set_state(self, state: ChaosState) -> ChaosState:
        if not self.allowed:
            raise ChaosDisabled("Chaos is not enabled in this environment")
        redis = self._client()
        if state.mode == "none":
            await redis.delete(STATE_KEY, REMAINING_KEY)
            return OFF
        await redis.set(STATE_KEY, json.dumps(state.as_dict()))
        if state.remaining_count is None:
            await redis.delete(REMAINING_KEY)
        else:
            await redis.set(REMAINING_KEY, state.remaining_count)
        return state

    async def reset(self) -> None:
        if not self.allowed:
            return
        try:
            await self._client().delete(STATE_KEY, REMAINING_KEY)
        except Exception as exc:
            log.warning("chaos reset failed: %s", type(exc).__name__)

    # ----------------------------------------------------------------- the hook
    async def _should_fire(self, state: ChaosState) -> bool:
        if state.mode == "none" or self._rng() >= state.probability:
            return False
        if state.remaining_count is None:
            return True
        try:
            redis = self._client()
            left = int(await redis.decr(REMAINING_KEY))
            if left < 0:  # budget already used up by another worker
                await redis.delete(STATE_KEY, REMAINING_KEY)
                return False
            if left == 0:  # this is the last one: switch chaos off afterwards
                await redis.delete(STATE_KEY, REMAINING_KEY)
        except Exception as exc:
            log.warning("chaos counter unavailable: %s", type(exc).__name__)
            return False
        return True

    async def before_send(self, operation: str) -> dict[str, str]:
        _lose_answer.set(False)
        state = await self.get_state()
        if state.mode == "none" or not await self._should_fire(state):
            return {}
        note_chaos_mode(state.mode)
        log.info("chaos fired: %s on %s", state.mode, operation)
        if state.mode == "delay":
            await self._sleep(state.delay_ms / 1000)
            return {}
        if state.mode == "drop_before_send":
            raise TransportLost("chaos: connection dropped before the request was sent")
        if state.mode == "drop_after_send":
            _lose_answer.set(True)
            return {}
        if state.mode == "mock_error":
            return {MOCK_HEADER: json.dumps({"mock_application_codes": state.code})}
        return {}

    async def after_send(self, operation: str) -> None:
        if _lose_answer.get():
            _lose_answer.set(False)
            raise TransportLost("chaos: the answer was lost after the request was delivered")