"""Central configuration for SlothPay.

Every setting comes from an environment variable (or a local .env file).
Secrets never go in git: only .env.example is committed.
"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_ONLY_ENVS = {"demo", "production"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- app ---
    ENV: str = "local"  # local | sandbox | demo | production
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # --- data stores ---
    DATABASE_URL: str = "postgresql+asyncpg://slothpay:slothpay@localhost:5432/slothpay"
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Temporal ---
    TEMPORAL_ADDRESS: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"

    # --- PayPal (SANDBOX ONLY) ---
    PAYPAL_CLIENT_ID: str = ""
    PAYPAL_CLIENT_SECRET: str = ""
    PAYPAL_BASE_URL: str = "https://api-m.sandbox.paypal.com"
    PAYPAL_WEBHOOK_ID: str = ""

    # --- monitoring ---
    SENTRY_DSN: str = ""

    # --- AI model (endpoint and name are configured, never hard-coded) ---
    GEMMA_BASE_URL: str = ""
    GEMMA_API_KEY: str = ""
    GEMMA_MODEL: str = ""

    # --- safety switches and secrets ---
    CHAOS_ENABLED: bool = False  # controlled failures; sandbox/local only
    ADMIN_API_KEY: str = ""
    PASSPORT_SIGNING_SECRET: str = ""

    # --- used by later tasks (kept here so there is one settings file) ---
    CONDITION_MAX_HOURS: int = 48  # Task 9: longest ConditionalPay wait
    ALLOWED_CURRENCIES: str = "USD,EUR,GBP,INR"  # Task 5: confirm INR works in the sandbox

    @property
    def currency_allowlist(self) -> list[str]:
        return [c.strip().upper() for c in self.ALLOWED_CURRENCIES.split(",") if c.strip()]

    @model_validator(mode="after")
    def _refuse_live_paypal_in_demo(self) -> "Settings":
        if self.ENV in SANDBOX_ONLY_ENVS and "sandbox" not in self.PAYPAL_BASE_URL.lower():
            raise ValueError(
                f"Refusing to start: ENV={self.ENV} but PAYPAL_BASE_URL is not the PayPal sandbox."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
