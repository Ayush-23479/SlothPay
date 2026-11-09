import os

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

_STATE: dict = {}


@pytest.fixture(autouse=True)
def _no_chaos_from_the_developers_machine(monkeypatch):
    """A PayPalClient built without an injector asks default_injector(), which turns chaos on
    when the developer's own .env says CHAOS_ENABLED=true and then reads the chaos state from
    the live local Redis. A mode armed in the dashboard ("Kill network") would then break an
    unrelated test. Tests get no chaos unless they install an injector themselves."""
    from backend.payments import hooks, paypal_client

    monkeypatch.setattr(paypal_client, "default_injector", lambda: hooks._override or hooks.NOOP)
    yield


_SKIP = "Temporal test server unavailable; set TEMPORAL_TEST_ADDRESS to a running dev server"


@pytest_asyncio.fixture
async def temporal():
    """(WorkflowEnvironment, time_skipping).

    Default: Temporal's time-skipping test server, which a day-long wait crosses instantly.
    Its binary is downloaded on first use. If that is impossible, set TEMPORAL_TEST_ADDRESS
    (for example localhost:7233 with `temporal server start-dev`) to use a real server on the
    real clock; the tests then use short deadlines. With neither, Temporal tests skip.
    """
    address = os.getenv("TEMPORAL_TEST_ADDRESS")
    if address:
        env = WorkflowEnvironment.from_client(await Client.connect(address))
        skipping = False
    elif _STATE.get("unavailable"):
        pytest.skip(_SKIP)
    else:
        try:
            env = await WorkflowEnvironment.start_time_skipping()
            skipping = True
        except Exception:
            _STATE["unavailable"] = True
            pytest.skip(_SKIP)
    try:
        yield env, skipping
    finally:
        await env.shutdown()