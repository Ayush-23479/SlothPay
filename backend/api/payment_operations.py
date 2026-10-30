"""Admin-only sandbox orchestration endpoints (not a merchant-facing API).

Task 9 will supply trusted condition proof. Until then conditional captures are
intentionally unavailable through HTTP.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config.settings import get_settings
from backend.orchestration.lifecycle import (
    LifecycleSafetyError,
    OperationAlreadyStarted,
    create_order,
    execute_operation,
    verify_buyer_approval,
)
from backend.payments.paypal_client import (
    PayPalClient,
    PayPalError,
    PayPalNotSent,
    PayPalOutcomeUnknown,
)

router = APIRouter(prefix="/internal/payments", tags=["internal-payment-operations"])


class OperationResult(BaseModel):
    state: str
    needs_reconciliation: bool = False
    paypal_order_id: str | None = None


async def get_session() -> AsyncIterator[AsyncSession]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def get_paypal() -> AsyncIterator[PayPalClient]:
    async with PayPalClient() as client:
        yield client


def require_admin(x_admin_api_key: Annotated[str | None, Header()] = None) -> None:
    configured = get_settings().ADMIN_API_KEY
    if (
        not configured
        or not x_admin_api_key
        or not hmac.compare_digest(configured, x_admin_api_key)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


Session = Annotated[AsyncSession, Depends(get_session)]
Gateway = Annotated[PayPalClient, Depends(get_paypal)]
Admin = Annotated[None, Depends(require_admin)]


def safe_result(payload: dict) -> OperationResult:
    return OperationResult(
        state=str(payload["state"]),
        needs_reconciliation=bool(payload.get("needs_reconciliation", False)),
        paypal_order_id=payload.get("paypal_order_id"),
    )


async def invoke(
    action, *, session: AsyncSession, paypal: PayPalClient, **kwargs
) -> OperationResult:
    try:
        return safe_result(await action(session, paypal, **kwargs))
    except OperationAlreadyStarted as exc:
        raise HTTPException(
            status_code=409, detail="Operation already started; reconcile before retry"
        ) from exc
    except LifecycleSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (PayPalNotSent, PayPalOutcomeUnknown, PayPalError) as exc:
        # Persisted attempt/state is authoritative. Never replay automatically.
        raise HTTPException(
            status_code=503,
            detail="Payment outcome requires inspection or reconciliation; no automatic retry",
        ) from exc


@router.post("/{payment_id}/create-order", response_model=OperationResult)
async def create_payment_order(
    payment_id: uuid.UUID, _admin: Admin, session: Session, paypal: Gateway
) -> OperationResult:
    return await invoke(create_order, session=session, paypal=paypal, payment_id=payment_id)


@router.post("/{payment_id}/verify-approval", response_model=OperationResult)
async def verify_approval(
    payment_id: uuid.UUID, _admin: Admin, session: Session, paypal: Gateway
) -> OperationResult:
    return await invoke(
        verify_buyer_approval, session=session, paypal=paypal, payment_id=payment_id
    )


@router.post("/{payment_id}/operations/{operation}", response_model=OperationResult)
async def run_payment_operation(
    payment_id: uuid.UUID,
    operation: Literal["authorize", "capture", "void"],
    _admin: Admin,
    session: Session,
    paypal: Gateway,
) -> OperationResult:
    # Do not accept a boolean condition_verified from HTTP. Even an admin header
    # is not trusted evidence that a seller fulfilled the purchase condition.
    # Conditional capture requires a separate server-side Task 9 proof workflow.
    if operation == "capture":
        from backend.models.payment import Payment

        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.mode == "CONDITIONALPAY":
            raise HTTPException(
                status_code=409,
                detail="Conditional capture requires trusted Task 9 condition proof",
            )
    return await invoke(
        execute_operation,
        session=session,
        paypal=paypal,
        payment_id=payment_id,
        operation=operation,
    )
