from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from sse_starlette.sse import EventSourceResponse

from backend.api.deps import (
    AnyCaller,
    Api,
    ApiError,
    Hub,
    IdempotencyKey,
    Owner,
    PaymentId,
    not_found,
    translate_errors,
)
from backend.api.schemas import (
    ConditionVerifyIn,
    ErrorOut,
    EventOut,
    PaymentCreate,
    PaymentListOut,
    PaymentOut,
    RescueActionIn,
    RescueActionOut,
    RescueOut,
)
from backend.notifications.stream import payment_event_stream

router = APIRouter(prefix="/api/payments", tags=["payments"])

HEARTBEAT_SECONDS = 15
_ERRORS = {
    400: {"model": ErrorOut},
    401: {"model": ErrorOut},
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    503: {"model": ErrorOut},
}


@router.post(
    "",
    response_model=PaymentOut,
    status_code=201,
    responses={
        200: {"model": PaymentOut, "description": "Replay of an earlier request"},
        **_ERRORS,
    },
)
async def create_payment(
    body: PaymentCreate,
    response: Response,
    owner: Owner,
    key: IdempotencyKey,
    api: Api,
) -> PaymentOut:
    """Start a payment. A repeat with the same Idempotency-Key returns the same payment."""
    with translate_errors():
        try:
            payment, replay = await api.create_payment(owner, key, body.to_payload())
        except ValueError as exc:
            raise ApiError(422, "invalid_request", str(exc)[:200]) from exc
    if replay:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return payment


@router.get("", response_model=PaymentListOut, responses=_ERRORS)
async def list_payments(
    owner: Owner,
    api: Api,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=80)] = None,
) -> PaymentListOut:
    with translate_errors():
        items, next_cursor = await api.list_payments(owner, limit, cursor)
    return PaymentListOut(items=items, next_cursor=next_cursor)


@router.get("/{payment_id}", response_model=PaymentOut, responses=_ERRORS)
async def get_payment(payment_id: PaymentId, owner: Owner, api: Api) -> PaymentOut:
    with translate_errors():
        payment = await api.get_payment(owner, payment_id)
    if payment is None:
        raise not_found()
    return payment


@router.get("/{payment_id}/events", response_model=list[EventOut], responses=_ERRORS)
async def list_events(
    payment_id: PaymentId,
    owner: Owner,
    api: Api,
    after_id: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    with translate_errors():
        events = await api.list_events(owner, payment_id, after_id, limit)
    if events is None:
        raise not_found()
    return events


@router.post("/{payment_id}/return", response_model=PaymentOut, responses=_ERRORS)
async def buyer_returned(payment_id: PaymentId, owner: Owner, api: Api) -> PaymentOut:
    """The buyer came back from PayPal. PayPal is asked what happened; the browser is not
    believed. Safe to call twice."""
    with translate_errors():
        payment = await api.buyer_returned(owner, payment_id)
    if payment is None:
        raise not_found()
    return payment


@router.post("/{payment_id}/conditions/verify", response_model=PaymentOut, responses=_ERRORS)
async def verify_condition(
    payment_id: PaymentId, body: ConditionVerifyIn, caller: AnyCaller, api: Api
) -> PaymentOut:
    """Owner or admin says whether the condition was met. Only `true` releases the funds."""
    with translate_errors():
        payment = await api.verify_condition(caller, payment_id, body.verified, body.evidence)
    if payment is None:
        raise not_found()
    return payment


@router.post("/{payment_id}/cancel", response_model=PaymentOut, responses=_ERRORS)
async def cancel_payment(payment_id: PaymentId, owner: Owner, api: Api) -> PaymentOut:
    with translate_errors():
        payment = await api.cancel(owner, payment_id)
    if payment is None:
        raise not_found()
    return payment


@router.get("/{payment_id}/rescue", response_model=RescueOut, responses=_ERRORS)
async def rescue_plan(payment_id: PaymentId, owner: Owner, api: Api) -> RescueOut:
    with translate_errors():
        plan = await api.rescue_plan(owner, payment_id)
    if plan is None:
        raise not_found()
    return plan


@router.post("/{payment_id}/rescue", response_model=RescueActionOut, responses=_ERRORS)
async def rescue_action(
    payment_id: PaymentId, body: RescueActionIn, owner: Owner, api: Api
) -> RescueActionOut:
    """Run one rescue action. It is checked against a fresh plan, so a stale button is refused."""
    with translate_errors():
        outcome = await api.rescue_action(owner, payment_id, body.action)
    if outcome is None:
        raise not_found()
    return RescueActionOut(**outcome)


@router.get("/{payment_id}/stream", responses=_ERRORS)
async def stream_payment(
    payment_id: PaymentId,
    owner: Owner,
    api: Api,
    hub: Hub,
    after_id: Annotated[str | None, Query(max_length=80)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=80)] = None,
) -> EventSourceResponse:
    """Live updates as server-sent events.

    `Last-Event-ID` (sent by the browser when it reconnects) or `?after_id=` (first connect)
    first replays the events the client missed. Without either, only new events are sent.
    A `: ping` comment arrives every 15 seconds so proxies keep the connection open."""
    cursor = last_event_id or after_id
    with translate_errors():
        if cursor is None:
            if await api.get_payment(owner, payment_id) is None:
                raise not_found()
            cursor = await api.latest_event_cursor(owner, payment_id)
        elif await api.list_events(owner, payment_id, cursor, 1) is None:
            raise not_found()

    async def fetch_events(after: str | None, limit: int) -> list[EventOut]:
        return await api.list_events(owner, payment_id, after, limit) or []

    async def fetch_payment() -> PaymentOut | None:
        return await api.get_payment(owner, payment_id)

    return EventSourceResponse(
        payment_event_stream(
            hub=hub,
            intent_id=str(payment_id),
            fetch_events=fetch_events,
            fetch_payment=fetch_payment,
            cursor=cursor,
        ),
        ping=HEARTBEAT_SECONDS,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )