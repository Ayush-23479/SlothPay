"""SlothPay API entry point.

Run:
    uvicorn backend.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.deps import install_error_handlers
from backend.api.payment_operations import router as payment_operations_router
from backend.api.payments_service import DbPaymentApi, LazyWorkflowStarter
from backend.api.routes_chaos import router as chaos_router
from backend.api.routes_extensions import router as extensions_router
from backend.api.routes_intelligence import router as intelligence_router
from backend.api.routes_metrics import router as metrics_router
from backend.api.routes_passport import router as passport_router
from backend.api.routes_payments import router as payments_router
from backend.api.routes_webhooks import router as webhooks_router
from backend.api.security import (
    WEBHOOK_MAX_BYTES,
    WEBHOOK_PATH,
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    rate_rules,
)
from backend.api.security import router as session_router
from backend.config.observability import init_observability
from backend.config.settings import get_settings
from backend.notifications.hub import close_hub, get_hub, get_redis
from backend.orchestration.inprocess_workers import BackgroundWorkers
from backend.payments.paypal_client import PayPalClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the real payment service. Nothing here connects yet: Postgres, Redis, PayPal and
    Temporal are each reached on first use, so /health answers even while one is down."""
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.session_factory = session_factory
    app.state.payment_api = DbPaymentApi(
        session_factory=session_factory,
        redis=get_redis(),
        hub=get_hub(),
        paypal_factory=PayPalClient,
        starter=LazyWorkflowStarter(),
        settings=settings,
    )
    workers: BackgroundWorkers | None = None
    if settings.RUN_BACKGROUND_WORKERS:
        workers = BackgroundWorkers(settings.BACKGROUND_WORKER_INTERVAL_SECONDS)
        workers.start()
    try:
        yield
    finally:
        if workers is not None:
            await workers.stop()
        app.state.payment_api = None
        app.state.session_factory = None
        await close_hub()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    init_observability(settings)  # Sentry; a no-op while SENTRY_DSN is empty

    hosted = settings.ENV in {"demo", "production"}
    app = FastAPI(
        title="SlothPay",
        version="0.1.0",
        lifespan=lifespan,
        # No public API explorer on a hosted demo.
        docs_url=None if hosted else "/docs",
        redoc_url=None if hosted else "/redoc",
        openapi_url=None if hosted else "/openapi.json",
    )

    # Middleware runs from the outside in: security headers (so even 413 and 429 carry them),
    # CORS, request size cap, rate limits. add_middleware wraps, so they are added in reverse.
    app.add_middleware(RateLimitMiddleware, redis_factory=get_redis, rules=rate_rules(settings))
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.MAX_REQUEST_BYTES,
        overrides={WEBHOOK_PATH: WEBHOOK_MAX_BYTES},
    )
    # Only the SlothPay frontend may call the API from a browser: one exact origin, no wildcard.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_ORIGIN],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Idempotency-Key",
            "X-Owner-Id",
            "X-Admin-Api-Key",
            "Last-Event-ID",
        ],
        expose_headers=["Idempotent-Replay", "Retry-After"],
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)
    install_error_handlers(app)

    app.include_router(session_router)  # Task 16: POST /api/demo-login, /api/demo-logout
    app.include_router(payment_operations_router)
    app.include_router(payments_router)  # Task 10
    app.include_router(webhooks_router)  # Task 7 receiver, mounted by Task 10
    app.include_router(metrics_router)  # Task 12
    app.include_router(intelligence_router)  # Task 13: invoice pre-fill
    app.include_router(passport_router)
    app.include_router(extensions_router)  # Task 18: Signed condition webhooks
    app.include_router(chaos_router)  # Task 12: answers 404 unless chaos is allowed

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "env": settings.ENV,
        }

    return app


app = create_app()