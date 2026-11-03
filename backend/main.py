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
from backend.api.routes_intelligence import router as intelligence_router
from backend.api.routes_metrics import router as metrics_router
from backend.api.routes_passport import router as passport_router
from backend.api.routes_payments import router as payments_router
from backend.api.routes_webhooks import router as webhooks_router
from backend.config.observability import init_observability
from backend.config.settings import get_settings
from backend.notifications.hub import close_hub, get_hub, get_redis
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
    try:
        yield
    finally:
        app.state.payment_api = None
        app.state.session_factory = None
        await close_hub()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    init_observability(settings)  # Sentry; a no-op while SENTRY_DSN is empty

    app = FastAPI(
        title="SlothPay",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Only the SlothPay frontend may call the API from a browser.
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
    install_error_handlers(app)

    app.include_router(payment_operations_router)
    app.include_router(payments_router)  # Task 10
    app.include_router(webhooks_router)  # Task 7 receiver, mounted by Task 10
    app.include_router(metrics_router)  # Task 12
    app.include_router(intelligence_router)  # Task 13: invoice pre-fill
    app.include_router(passport_router)  # Task 14: Trust Passport
    app.include_router(chaos_router)  # Task 12: answers 404 unless chaos is allowed

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "env": settings.ENV,
        }

    return app


app = create_app()