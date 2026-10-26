from fastapi.testclient import TestClient

from backend.config.settings import get_settings
from backend.main import create_app


def test_health_returns_ok_and_env(monkeypatch):
    monkeypatch.setenv("ENV", "local")
    get_settings.cache_clear()
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "env": "local"}
    get_settings.cache_clear()
