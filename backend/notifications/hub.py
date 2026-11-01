from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from redis.asyncio import Redis

from backend.config.settings import get_settings

log = logging.getLogger("slothpay.hub")

MAX_MESSAGE_BYTES = 4096
CONFIRM_SECONDS = 2.0


def channel_for(intent_id: str) -> str:
    """The Redis channel for one payment. Only a real UUID is accepted."""
    return f"payments:{uuid.UUID(str(intent_id))}"


class Subscription(Protocol):
    async def next(self, timeout: float) -> dict[str, Any] | None:
        """The next event, or None if nothing arrived within `timeout` seconds."""
        ...


class EventHub(Protocol):
    async def publish(self, intent_id: str, event: dict[str, Any]) -> bool: ...

    def subscription(self, intent_id: str) -> AbstractAsyncContextManager[Subscription]: ...


async def subscribe(hub: EventHub, intent_id: str, *, idle: float = 1.0) -> AsyncIterator[dict]:
    """Async-iterate the events of one payment until the caller stops iterating."""
    async with hub.subscription(intent_id) as sub:
        while True:
            event = await sub.next(idle)
            if event is not None:
                yield event


def _encode(event: dict[str, Any]) -> str | None:
    try:
        text = json.dumps(event, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None
    return text if len(text.encode()) <= MAX_MESSAGE_BYTES else None


def _decode(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}  # an unreadable message is still a valid wake-up
    return value if isinstance(value, dict) else {}


# ------------------------------------------------------------------------------- Redis
class _RedisSubscription:
    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub

    async def next(self, timeout: float) -> dict[str, Any] | None:
        message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if message is None:
            return None
        return _decode(message.get("data"))


class RedisHub:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def publish(self, intent_id: str, event: dict[str, Any]) -> bool:
        text = _encode(event)
        if text is None:
            text = "{}"
        try:
            await self.redis.publish(channel_for(intent_id), text)
        except Exception:
            log.exception("could not publish a live update for payment %s", intent_id)
            return False
        return True

    @asynccontextmanager
    async def subscription(self, intent_id: str) -> AsyncIterator[Subscription]:
        channel = channel_for(intent_id)
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            # SUBSCRIBE is only in effect once Redis confirms it. Wait for that, so an event
            # published right after this returns (or between our database read and our wait)
            # cannot slip past us.
            async with asyncio.timeout(CONFIRM_SECONDS):
                while True:
                    message = await pubsub.get_message(timeout=CONFIRM_SECONDS)
                    if message is not None and message.get("type") == "subscribe":
                        break
            yield _RedisSubscription(pubsub)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:  # the connection may already be gone
                log.debug("pubsub cleanup failed", exc_info=True)


# ---------------------------------------------------------------------------- in memory
class _QueueSubscription:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    async def next(self, timeout: float) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None


class InMemoryHub:
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue]] = {}

    def subscriber_count(self, intent_id: str) -> int:
        return len(self._queues.get(channel_for(intent_id), ()))

    async def publish(self, intent_id: str, event: dict[str, Any]) -> bool:
        for queue in list(self._queues.get(channel_for(intent_id), ())):
            queue.put_nowait(dict(event))
        return True

    @asynccontextmanager
    async def subscription(self, intent_id: str) -> AsyncIterator[Subscription]:
        channel = channel_for(intent_id)
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(channel, set()).add(queue)
        try:
            yield _QueueSubscription(queue)
        finally:
            self._queues[channel].discard(queue)
            if not self._queues[channel]:
                del self._queues[channel]


# ------------------------------------------------------------- process-wide instances
_redis: Redis | None = None
_hub: EventHub | None = None


def get_redis() -> Redis:
    """The shared Redis client (locks and live updates). Connects lazily on first use."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _redis


def get_hub() -> EventHub:
    global _hub
    if _hub is None:
        _hub = RedisHub(get_redis())
    return _hub


def set_hub(hub: EventHub | None) -> None:
    """Replace the process-wide hub (tests, or a local run without Redis)."""
    global _hub
    _hub = hub


async def close_hub() -> None:
    global _redis, _hub
    redis, _redis, _hub = _redis, None, None
    if redis is not None:
        await redis.aclose()