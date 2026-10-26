import pytest
from pydantic import ValidationError

from backend.config.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)


def make(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_defaults_are_safe_for_local_use():
    s = make()
    assert s.ENV == "local"
    assert "sandbox" in s.PAYPAL_BASE_URL
    assert s.CHAOS_ENABLED is False


def test_chaos_defaults_to_false_even_with_other_settings():
    assert make(ENV="sandbox").CHAOS_ENABLED is False


@pytest.mark.parametrize("env", ["demo", "production"])
def test_refuses_live_paypal_url_in_demo_and_production(env):
    with pytest.raises(ValidationError):
        make(ENV=env, PAYPAL_BASE_URL="https://api-m.paypal.com")


@pytest.mark.parametrize("env", ["demo", "production"])
def test_accepts_sandbox_url_in_demo_and_production(env):
    s = make(ENV=env, PAYPAL_BASE_URL="https://api-m.sandbox.paypal.com")
    assert s.ENV == env


def test_local_may_use_any_url_without_refusing():
    assert make(ENV="local", PAYPAL_BASE_URL="https://example.test").ENV == "local"


def test_currency_allowlist_is_parsed_and_uppercased():
    assert make(ALLOWED_CURRENCIES="usd, eur ,inr").currency_allowlist == ["USD", "EUR", "INR"]


def test_reads_values_from_environment(monkeypatch):
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    assert make().TEMPORAL_ADDRESS == "temporal:7233"


def test_get_settings_is_cached():
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
