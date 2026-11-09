from __future__ import annotations

import pytest

from backend import main as main_module
from backend.config.settings import Settings, get_settings


class SpyWorkers:
    instances: list[SpyWorkers] = []

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.started = False
        self.stopped = False
        SpyWorkers.instances.append(self)

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)  # ignore the developer's .env
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("PASSPORT_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setattr(main_module, "BackgroundWorkers", SpyWorkers)
    SpyWorkers.instances.clear()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_workers_are_off_by_default():
    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        assert SpyWorkers.instances == []
        assert app.state.payment_api is not None


async def test_workers_start_with_the_api_and_stop_when_it_shuts_down(monkeypatch):
    monkeypatch.setenv("RUN_BACKGROUND_WORKERS", "true")
    monkeypatch.setenv("BACKGROUND_WORKER_INTERVAL_SECONDS", "7")
    get_settings.cache_clear()
    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        (workers,) = SpyWorkers.instances
        assert workers.started and not workers.stopped
        assert workers.interval == 7.0
    assert workers.stopped
    assert app.state.payment_api is None


async def test_workers_are_stopped_even_if_the_app_crashes(monkeypatch):
    monkeypatch.setenv("RUN_BACKGROUND_WORKERS", "true")
    get_settings.cache_clear()
    app = main_module.create_app()
    with pytest.raises(RuntimeError, match="boom"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("boom")
    assert SpyWorkers.instances[0].stopped


async def test_health_answers_without_any_service_while_workers_run(monkeypatch):
    import httpx

    monkeypatch.setenv("RUN_BACKGROUND_WORKERS", "true")
    get_settings.cache_clear()
    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"