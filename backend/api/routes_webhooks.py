from __future__ import annotations

from fastapi import APIRouter

from backend.webhooks.receiver import router as paypal_webhook_router

router = APIRouter()
router.include_router(paypal_webhook_router)  # POST /webhooks/paypal