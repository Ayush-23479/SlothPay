from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Request

from backend.reliability import timing

log = logging.getLogger("slothpay.timing")


async def _finish(timer: timing.StepTimer, app: Any) -> None:
    try:
        await timing.persist(getattr(app.state, "session_factory", None), timer)
    finally:
        await timing.run_after_response(timer)


async def request_timing(request: Request) -> AsyncIterator[timing.StepTimer]:
    timer = timing.StepTimer()
    token = timing.activate(timer)
    try:
        yield timer
    finally:
        # Runs when the handler ends, on success and on an error response alike.
        timer.stop()
        timing.deactivate(token)
        try:
            timing.spawn(_finish(timer, request.app), "request-timing-finish")
        except RuntimeError:  # no running loop: nothing to schedule
            pass


Timed = Annotated[timing.StepTimer, Depends(request_timing)]