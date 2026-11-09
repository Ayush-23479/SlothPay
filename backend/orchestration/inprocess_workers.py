from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("slothpay.workers")

Step = Callable[[], Awaitable[object]]


async def run_forever(name: str, step: Step, interval: float, stop: asyncio.Event) -> None:
    """Call `step` again and again until `stop` is set. A failing step is logged and retried
    on the next round; it never ends the loop."""
    failing = False
    while not stop.is_set():
        try:
            await step()
            failing = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The full traceback once per streak of failures; later rounds stay one short line.
            log.warning(
                "background step %s failed (%s); trying again next round",
                name,
                type(exc).__name__,
                exc_info=not failing,
            )
            failing = True
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def default_steps() -> dict[str, Step]:
    # Imported here so merely importing this module never needs a database or PayPal setup.
    from backend.orchestration.recovery_worker import process_batch as recovery_batch
    from backend.webhooks.worker import process_batch as webhook_batch

    return {"recovery": recovery_batch, "webhooks": webhook_batch}


class BackgroundWorkers:
    """Starts the loops and stops them cleanly when the API shuts down."""

    def __init__(self, interval: float, steps: dict[str, Step] | None = None) -> None:
        self._interval = interval
        self._steps = steps if steps is not None else default_steps()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        for name, step in self._steps.items():
            self._tasks.append(
                asyncio.create_task(
                    run_forever(name, step, self._interval, self._stop), name=f"slothpay-{name}"
                )
            )
        log.info("background workers started: %s", ", ".join(self._steps))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()