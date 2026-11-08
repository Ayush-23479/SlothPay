import math
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_ONLY_ENVS = {"demo", "production"}
KNOWN_ENVS = {"local", "sandbox", "demo", "production"}
CHAOS_ALLOWED_ENVS = {"local", "sandbox"}
MIN_SECRET_LENGTH = 32
# Anything that still looks like the .env.example placeholder is refused in demo and production.
_PLACEHOLDER_MARKERS = ("change-me", "changeme", "your-", "example", "placeholder")


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
    # --- Task 16: sessions, rate limits and request size ---
    SESSION_TTL_HOURS: int = 12  # life of the signed demo session cookie
    DEMO_USERS: str = "alice,bob,carol"  # named demo identities POST /api/demo-login may issue
    RATE_LIMIT_PAYMENTS_PER_MINUTE: int = 30  # POST /api/payments, per client address
    RATE_LIMIT_WEBHOOK_PER_MINUTE: int = 300  # POST /webhooks/paypal, per client address
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 20  # POST /api/demo-login, per client address
    MAX_REQUEST_BYTES: int = 64 * 1024  # request body cap (the webhook route has its own 256 KB)

    CONDITION_MAX_HOURS: int = 48  # Task 9: longest ConditionalPay wait
    ALLOWED_CURRENCIES: str = "USD,EUR,GBP,INR"  # Task 5: confirm INR works in the sandbox
    RECONCILIATION_DELAYS_SECONDS: str = "0.3,1,2,4,8"

    @property
    def owner_header_allowed(self) -> bool:
        """The X-Owner-Id header is a local-development shortcut. Anywhere else the identity
        comes only from the signed session cookie."""
        return self.ENV == "local"

    @property
    def secure_cookies(self) -> bool:
        return self.ENV != "local"

    @property
    def demo_user_names(self) -> list[str]:
        return [name.strip().lower() for name in self.DEMO_USERS.split(",") if name.strip()]

    @property
    def currency_allowlist(self) -> list[str]:
        return [c.strip().upper() for c in self.ALLOWED_CURRENCIES.split(",") if c.strip()]

    @property
    def reconciliation_delays_seconds(self) -> tuple[float, ...]:
        return tuple(
            float(value.strip()) for value in self.RECONCILIATION_DELAYS_SECONDS.split(",")
        )

    @model_validator(mode="after")
    def _refuse_unsafe_configuration(self) -> "Settings":
        if self.ENV not in KNOWN_ENVS:
            raise ValueError(f"Refusing to start: ENV must be one of {sorted(KNOWN_ENVS)}.")
        if self.ENV in SANDBOX_ONLY_ENVS and not _is_paypal_sandbox_url(self.PAYPAL_BASE_URL):
            raise ValueError(
                f"Refusing to start: ENV={self.ENV} but PAYPAL_BASE_URL is not the PayPal sandbox."
            )
        if self.ENV in SANDBOX_ONLY_ENVS:
            for name in ("ADMIN_API_KEY", "PASSPORT_SIGNING_SECRET"):
                value = getattr(self, name)
                if len(value) < MIN_SECRET_LENGTH or any(
                    marker in value.lower() for marker in _PLACEHOLDER_MARKERS
                ):
                    raise ValueError(
                        f"Refusing to start: {name} must be a real random secret of at least "
                        f"{MIN_SECRET_LENGTH} characters when ENV={self.ENV}."
                    )
        if self.CHAOS_ENABLED and self.ENV not in CHAOS_ALLOWED_ENVS:
            raise ValueError(
                f"Refusing to start: CHAOS_ENABLED is true but ENV={self.ENV}; "
                "controlled failures are allowed only in local or sandbox."
            )
        if not 1 <= self.SESSION_TTL_HOURS <= 168:
            raise ValueError("SESSION_TTL_HOURS must be between 1 and 168")
        if not 1024 <= self.MAX_REQUEST_BYTES <= 1024 * 1024:
            raise ValueError("MAX_REQUEST_BYTES must be between 1 KB and 1 MB")
        try:
            delays = self.reconciliation_delays_seconds
        except ValueError as exc:
            raise ValueError("RECONCILIATION_DELAYS_SECONDS must contain five numbers") from exc
        if (
            len(delays) != 5
            or any(not math.isfinite(delay) or delay <= 0 for delay in delays)
            or any(
                current >= following for current, following in zip(delays, delays[1:], strict=False)
            )
        ):
            raise ValueError(
                "RECONCILIATION_DELAYS_SECONDS must contain five positive, increasing delays"
            )
        return self


def _is_paypal_sandbox_url(value: str) -> bool:
    """True only for https URLs whose host is PayPal's sandbox. A path or a look-alike host
    that merely contains the word "sandbox" does not count."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host.endswith(".sandbox.paypal.com")


@lru_cache
def get_settings() -> Settings:
    return Settings()