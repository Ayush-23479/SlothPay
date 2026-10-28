"""SlothPay API entry point.

Run locally:
    uvicorn backend.main:app --reload
"""

from fastapi import FastAPI

from backend.config.settings import get_settings
from backend.webhooks.router import router as paypal_webhook_router


def create_app() -> FastAPI:
    """Create and configure the SlothPay FastAPI application."""

    settings = get_settings()

    app = FastAPI(
        title="SlothPay",
        version="0.1.0",
        description=(
            "SlothPay payment orchestration API with "
            "SmartPay, ConditionalPay, and PayPal Sandbox webhooks."
        ),
    )

    # Task 7: Register PayPal webhook endpoints.
    app.include_router(paypal_webhook_router)

    @app.get("/health")
    def health() -> dict:
        """Return the application's health status."""
        return {
            "status": "ok",
            "env": settings.ENV,
        }

    return app


app = create_app()