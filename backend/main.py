"""SlothPay API entry point. Run: uvicorn backend.main:app --reload"""

from fastapi import FastAPI

from backend.config.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()  # raises if the configuration is unsafe
    app = FastAPI(title="SlothPay", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "env": settings.ENV}

    return app


app = create_app()
