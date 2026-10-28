"""SlothPay API entry point.

Run:
    uvicorn backend.main:app --reload
"""

from fastapi import FastAPI

from backend.api.payment_operations import router as payment_operations_router
from backend.config.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="SlothPay",
        version="0.1.0",
    )

    app.include_router(payment_operations_router)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "env": settings.ENV,
        }

    return app


app = create_app()