import uuid

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI, Header, Response

from backend.api.deps import install_error_handlers
from backend.api.routes_chaos import MAX_DOUBLE_CLICKS
from backend.api.routes_chaos import router as chaos_router
from backend.api.routes_metrics import get_session_factory
from backend.api.routes_metrics import router as metrics_router
from backend.config import observability
from backend.config.settings import Settings
from backend.payments import hooks
from backend.payments.hooks import NoopInjector, TransportLost, take_chaos_mode
from backend.payments.paypal_client import PayPalClient, PayPalNotSent, PayPalOutcomeUnknown
from backend.reliability.chaos import (
    STATE_KEY,
    ChaosController,
    ChaosDisabled,
    ChaosInvalid,
    chaos_allowed,
    validate_state,
)

ADMIN = "admin-key-for-tests"


def settings(**overrides) -> Settings:
    values = {"ENV": "local", "CHAOS_ENABLED": True, "ADMIN_API_KEY": ADMIN}
    values.update(overrides)
    if values["ENV"] in {"demo", "production"}:
        # Since Task 16 Settings refuses to start with chaos switched on in a protected
        # environment. These tests exercise the SECOND line of defence (chaos_allowed and the
        # routes), so they build that impossible configuration without validation.
        return Settings.model_construct(**values)
    return Settings(_env_file=None, **values)


def controller(redis=None, *, rng=lambda: 0.0, **overrides):
    return ChaosController(
        settings(**overrides), redis or FakeAsyncRedis(decode_responses=True), rng=rng
    )


async def activate(ctrl: ChaosController, mode: str, **kw):
    await ctrl.set_state(validate_state(mode, **kw))


@pytest.fixture(autouse=True)
def _clean_context():
    take_chaos_mode()
    yield
    take_chaos_mode()
    hooks.set_default_injector(None)


# ------------------------------------------------------------------------- allowed or not
@pytest.mark.parametrize(
    ("env", "enabled", "allowed"),
    [
        ("local", True, True),
        ("sandbox", True, True),
        ("local", False, False),
        ("demo", True, False),
        ("production", True, False),
    ],
)
def test_chaos_is_only_allowed_in_local_or_sandbox_when_enabled(env, enabled, allowed):
    cfg = settings(ENV=env, CHAOS_ENABLED=enabled)
    assert chaos_allowed(cfg) is allowed


async def test_chaos_is_a_no_op_when_disabled_even_if_a_mode_is_stored():
    redis = FakeAsyncRedis(decode_responses=True)
    # Someone left a mode in Redis (or wrote it directly): a disabled controller ignores it.
    await redis.set(STATE_KEY, '{"mode": "drop_after_send", "probability": 1.0}')
    for overrides in ({"CHAOS_ENABLED": False}, {"ENV": "demo"}):
        ctrl = controller(redis, **overrides)
        assert (await ctrl.get_state()).mode == "none"
        assert await ctrl.before_send("capture_order") == {}
        await ctrl.after_send("capture_order")  # must not raise
        assert take_chaos_mode() is None
        with pytest.raises(ChaosDisabled):
            await activate(ctrl, "delay", delay_ms=10)


def test_default_injector_is_a_no_op_unless_chaos_is_allowed(monkeypatch):
    monkeypatch.setattr("backend.config.settings.get_settings", lambda: settings(ENV="demo"))
    assert isinstance(hooks.default_injector(), NoopInjector)
    monkeypatch.setattr("backend.config.settings.get_settings", lambda: settings())
    assert isinstance(hooks.default_injector(), ChaosController)


# ------------------------------------------------------------------------------ the modes
async def test_delay_sleeps_for_delay_ms():
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    ctrl = ChaosController(
        settings(), FakeAsyncRedis(decode_responses=True), rng=lambda: 0.0, sleep=fake_sleep
    )
    await activate(ctrl, "delay", delay_ms=250)
    assert await ctrl.before_send("create_order") == {}
    assert slept == [0.25]
    assert take_chaos_mode() == "delay"


async def test_mock_error_adds_the_paypal_mock_header():
    ctrl = controller()
    await activate(ctrl, "mock_error", code="INSTRUMENT_DECLINED")
    headers = await ctrl.before_send("capture_order")
    assert headers == {"PayPal-Mock-Response": '{"mock_application_codes": "INSTRUMENT_DECLINED"}'}
    assert take_chaos_mode() == "mock_error"


async def test_probability_zero_never_fires_and_one_always_does():
    quiet = controller(rng=lambda: 0.5)
    await activate(quiet, "drop_before_send", probability=0.0)
    for _ in range(5):
        assert await quiet.before_send("capture_order") == {}
    assert take_chaos_mode() is None
    loud = controller(rng=lambda: 0.999)
    await activate(loud, "drop_before_send", probability=1.0)
    with pytest.raises(TransportLost):
        await loud.before_send("capture_order")


async def test_remaining_count_limits_how_many_failures_fire_then_switches_off():
    ctrl = controller()
    await activate(ctrl, "drop_before_send", remaining_count=2)
    fired = 0
    for _ in range(5):
        try:
            await ctrl.before_send("capture_order")
        except TransportLost:
            fired += 1
    assert fired == 2
    assert (await ctrl.get_state()).mode == "none"


async def test_a_redis_outage_means_no_chaos_not_an_error():
    class BrokenRedis:
        async def get(self, *_):
            raise ConnectionError("redis is down")

    ctrl = ChaosController(settings(), BrokenRedis(), rng=lambda: 0.0)
    assert await ctrl.before_send("capture_order") == {}
    await ctrl.after_send("capture_order")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "explode"},
        {"mode": "delay", "probability": 1.5},
        {"mode": "delay", "probability": -0.1},
        {"mode": "delay", "delay_ms": 10_001},
        {"mode": "delay", "remaining_count": 0},
        {"mode": "mock_error"},
        {"mode": "mock_error", "code": "bad code!"},
        {"mode": "delay", "probability": True},
    ],
)
def test_invalid_settings_are_rejected(kwargs):
    with pytest.raises(ChaosInvalid):
        validate_state(**kwargs)


# ------------------------------------------------------------ through the real PayPalClient
TOKEN = "/v1/oauth2/token"


def recording_client(injector, log):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        log.append(("paypal_received", request.method, request.url.path))
        log.append(("headers", dict(request.headers)))
        return httpx.Response(201, json={"id": "ORDER1", "status": "CREATED"})

    return PayPalClient(
        client_id="id",
        client_secret="secret",
        base_url="https://api-m.sandbox.paypal.com",
        transport=httpx.MockTransport(respond),
        injector=injector,
    )


class OrderedInjector:
    """Wraps the chaos controller and records when each hook runs relative to the call."""

    def __init__(self, inner, log):
        self.inner, self.log = inner, log

    async def before_send(self, operation):
        self.log.append(("before_send", operation))
        return await self.inner.before_send(operation)

    async def after_send(self, operation):
        self.log.append(("after_send", operation))
        try:
            await self.inner.after_send(operation)
        except TransportLost:
            self.log.append(("lost", operation))
            raise


async def test_drop_after_send_raises_only_after_the_call_completed():
    log: list = []
    ctrl = controller()
    await activate(ctrl, "drop_after_send")
    paypal = recording_client(OrderedInjector(ctrl, log), log)
    with pytest.raises(PayPalOutcomeUnknown):
        await paypal.create_order(intent="CAPTURE", amount="10.00", currency="USD", request_id="r1")
    names = [entry[0] for entry in log]
    # PayPal received the request FIRST, and only then was the answer lost.
    assert names == ["before_send", "paypal_received", "headers", "after_send", "lost"]
    assert take_chaos_mode() == "drop_after_send"
    await paypal.aclose()


async def test_drop_before_send_never_reaches_paypal_and_is_safe_to_retry():
    log: list = []
    ctrl = controller()
    await activate(ctrl, "drop_before_send")
    paypal = recording_client(OrderedInjector(ctrl, log), log)
    with pytest.raises(PayPalNotSent):
        await paypal.create_order(intent="CAPTURE", amount="10.00", currency="USD", request_id="r1")
    assert not any(entry[0] == "paypal_received" for entry in log)
    await paypal.aclose()


async def test_mock_error_header_reaches_paypal_on_the_money_call_only():
    log: list = []
    ctrl = controller()
    await activate(ctrl, "mock_error", code="INSTRUMENT_DECLINED")
    paypal = recording_client(ctrl, log)
    await paypal.create_order(intent="CAPTURE", amount="10.00", currency="USD", request_id="r1")
    sent = next(entry[1] for entry in log if entry[0] == "headers")
    assert "INSTRUMENT_DECLINED" in sent["paypal-mock-response"]
    await paypal.aclose()


async def test_normal_calls_are_untouched_when_chaos_is_off():
    log: list = []
    paypal = recording_client(controller(), log)  # nothing activated
    result = await paypal.create_order(
        intent="CAPTURE", amount="10.00", currency="USD", request_id="r1"
    )
    assert result["id"] == "ORDER1"
    sent = next(entry[1] for entry in log if entry[0] == "headers")
    assert "paypal-mock-response" not in sent
    await paypal.aclose()


# ------------------------------------------------------------------------ Sentry scrubbing
def test_scrubber_removes_authorization_bodies_and_emails():
    event = {
        "message": "failed for alice@example.com",
        "request": {
            "url": "https://api.example/pay",
            "headers": {
                "Authorization": "Bearer secret-token",
                "X-Admin-Api-Key": "admin",
                "Accept": "application/json",
            },
            "data": {"payer": "bob@example.org", "amount": "10.00"},
            "cookies": {"slothpay_owner": "abc"},
        },
        "user": {"email": "carol@example.com"},
        "extra": {"nested": [{"note": "mail dave@mail.co.in now"}]},
        "tags": {"intent_id": "11111111-1111-1111-1111-111111111111"},
    }
    clean = observability.scrub_event(event)
    flat = repr(clean)
    assert "secret-token" not in flat
    assert "admin" not in flat.replace("Admin", "")  # the key value, not the header name
    assert "@" not in flat
    assert "data" not in clean["request"]
    assert "cookies" not in clean["request"]
    assert "user" not in clean
    assert clean["request"]["headers"]["Authorization"] == observability.REDACTED
    assert clean["request"]["headers"]["Accept"] == "application/json"
    assert clean["tags"]["intent_id"] == "11111111-1111-1111-1111-111111111111"


def test_scrubber_handles_header_lists_and_lower_case_names():
    event = {"request": {"headers": [["authorization", "Bearer x"], ["accept", "*/*"]]}}
    clean = observability.scrub_event(event)
    assert clean["request"]["headers"][0] == ["authorization", observability.REDACTED]
    assert clean["request"]["headers"][1] == ["accept", "*/*"]


def test_observability_is_a_no_op_without_a_dsn(monkeypatch):
    monkeypatch.setattr(observability, "_enabled", False)
    assert observability.init_observability(settings(SENTRY_DSN="")) is False
    observability.tag_event("gateway", "capture_order", "abc")
    observability.report("UNCERTAIN entered", "warning", layer="safety")
    assert observability.is_enabled() is False


def test_report_sends_tagged_messages_only_when_enabled(monkeypatch):
    import sentry_sdk

    seen = []

    def fake_capture(message, level=None, **_):
        tags = dict(sentry_sdk.get_current_scope()._tags)
        seen.append((message, level, tags))

    monkeypatch.setattr(sentry_sdk, "capture_message", fake_capture)
    monkeypatch.setattr(observability, "_enabled", True)
    observability.report(
        "UNCERTAIN entered",
        "warning",
        layer="safety",
        operation="capture_order",
        intent_id="abc-123",
        owner="someone@example.com",
    )
    observability.report("bad layer", layer="not-a-layer")
    assert seen[0][0] == "UNCERTAIN entered"
    assert seen[0][1] == "warning"
    assert seen[0][2]["layer"] == "safety"
    assert seen[0][2]["operation"] == "capture_order"
    assert seen[0][2]["intent_id"] == "abc-123"
    assert "@" not in seen[0][2]["owner"]
    assert "layer" not in seen[1][2]


def test_init_with_a_dsn_turns_sentry_on_with_a_modest_sample_rate(monkeypatch):
    import sentry_sdk

    captured = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    monkeypatch.setattr(observability, "_enabled", False)
    cfg = settings(SENTRY_DSN="https://public@o0.ingest.sentry.io/1", ENV="sandbox")
    assert observability.init_observability(cfg) is True
    assert captured["send_default_pii"] is False
    assert captured["environment"] == "sandbox"
    assert 0 < captured["traces_sample_rate"] <= 0.25
    assert captured["before_send"] is observability.scrub_event
    monkeypatch.setattr(observability, "_enabled", False)


# --------------------------------------------------------------------------- the routes
def chaos_app(monkeypatch, cfg: Settings, redis=None) -> FastAPI:
    redis = redis or FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr("backend.api.routes_chaos.get_settings", lambda: cfg)
    monkeypatch.setattr("backend.notifications.hub.get_redis", lambda: redis)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(chaos_router)
    created: dict[str, dict] = {}

    @app.post("/api/payments", status_code=201)
    async def fake_create(
        body: dict,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ):
        if idempotency_key in created:
            response.headers["Idempotent-Replay"] = "true"
            return created[idempotency_key]
        created[idempotency_key] = {
            "id": str(uuid.uuid4()),
            "paypal_ids": {"order_id": "ORDE…EF-1"},
        }
        return created[idempotency_key]

    return app


def api(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_chaos_routes_return_404_when_chaos_is_disabled(monkeypatch):
    app = chaos_app(monkeypatch, settings(CHAOS_ENABLED=False))
    async with api(app) as http:
        for method, path in (
            ("get", "/api/chaos"),
            ("post", "/api/chaos"),
            ("post", "/api/chaos/double-click"),
        ):
            response = await http.request(
                method.upper(), path, headers={"X-Admin-Api-Key": ADMIN}, json={}
            )
            assert response.status_code == 404, path


async def test_chaos_routes_return_404_in_a_protected_environment(monkeypatch):
    app = chaos_app(monkeypatch, settings(ENV="demo"))
    async with api(app) as http:
        response = await http.get("/api/chaos", headers={"X-Admin-Api-Key": ADMIN})
    assert response.status_code == 404


async def test_chaos_routes_return_401_without_the_admin_key(monkeypatch):
    app = chaos_app(monkeypatch, settings())
    async with api(app) as http:
        for headers in ({}, {"X-Admin-Api-Key": "wrong"}, {"X-Admin-Api-Key": ""}):
            for method, path in (
                ("get", "/api/chaos"),
                ("post", "/api/chaos"),
                ("post", "/api/chaos/double-click"),
            ):
                response = await http.request(method.upper(), path, headers=headers, json={})
                assert response.status_code == 401, (path, headers)
                assert response.json()["error"] == "unauthorized"


async def test_an_unset_admin_key_never_matches(monkeypatch):
    app = chaos_app(monkeypatch, settings(ADMIN_API_KEY=""))
    async with api(app) as http:
        response = await http.get("/api/chaos", headers={"X-Admin-Api-Key": ""})
    assert response.status_code == 401


async def test_the_admin_can_read_and_set_the_failure_mode(monkeypatch):
    app = chaos_app(monkeypatch, settings())
    headers = {"X-Admin-Api-Key": ADMIN}
    async with api(app) as http:
        assert (await http.get("/api/chaos", headers=headers)).json()["mode"] == "none"
        response = await http.post(
            "/api/chaos",
            headers=headers,
            json={"mode": "drop_after_send", "probability": 0.5, "remaining_count": 3},
        )
        assert response.status_code == 200
        assert response.json()["mode"] == "drop_after_send"
        assert response.json()["remaining_count"] == 3
        assert (await http.get("/api/chaos", headers=headers)).json()["probability"] == 0.5
        off = await http.post("/api/chaos", headers=headers, json={"mode": "none"})
        assert off.json()["mode"] == "none"


async def test_bad_chaos_settings_get_a_clear_error(monkeypatch):
    app = chaos_app(monkeypatch, settings())
    headers = {"X-Admin-Api-Key": ADMIN}
    async with api(app) as http:
        missing_code = await http.post("/api/chaos", headers=headers, json={"mode": "mock_error"})
        assert missing_code.status_code == 422
        assert missing_code.json()["error"] == "invalid_chaos"
        too_slow = await http.post(
            "/api/chaos", headers=headers, json={"mode": "delay", "delay_ms": 999_999}
        )
        assert too_slow.status_code == 422
        unknown = await http.post("/api/chaos", headers=headers, json={"mode": "explode"})
        assert unknown.status_code == 422


PAYMENT = {"path": "SMARTPAY", "amount": "10.00", "currency": "USD", "description": None}


async def test_double_click_reports_one_intent_and_one_order(monkeypatch):
    app = chaos_app(monkeypatch, settings())
    async with api(app) as http:
        response = await http.post(
            "/api/chaos/double-click",
            headers={"X-Admin-Api-Key": ADMIN},
            json={"n": 8, "payment": PAYMENT},
        )
    body = response.json()
    assert response.status_code == 200
    assert body["requested"] == 8
    assert body["accepted"] == 8
    assert body["intents"] == 1
    assert body["paypal_orders"] == 1
    assert body["replays"] == 7
    assert body["safe"] is True


async def test_double_click_is_capped_at_twenty(monkeypatch):
    app = chaos_app(monkeypatch, settings())
    async with api(app) as http:
        response = await http.post(
            "/api/chaos/double-click",
            headers={"X-Admin-Api-Key": ADMIN},
            json={"n": MAX_DOUBLE_CLICKS + 1, "payment": PAYMENT},
        )
    assert response.status_code == 422


async def test_double_click_flags_an_unsafe_backend(monkeypatch):
    """If the API ever made one payment per click, the demo must say so."""
    app = chaos_app(monkeypatch, settings())
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/api/payments"]

    @app.post("/api/payments", status_code=201)
    async def broken_create(body: dict):
        return {"id": str(uuid.uuid4()), "paypal_ids": {"order_id": str(uuid.uuid4())}}

    async with api(app) as http:
        response = await http.post(
            "/api/chaos/double-click",
            headers={"X-Admin-Api-Key": ADMIN},
            json={"n": 3, "payment": PAYMENT},
        )
    assert response.json()["intents"] == 3
    assert response.json()["safe"] is False


# -------------------------------------------------------------------------- metrics route
async def test_metrics_is_unavailable_without_a_database():
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(metrics_router)
    async with api(app) as http:
        response = await http.get("/api/metrics", headers={"X-Owner-Id": "metrics-viewer"})
    assert response.status_code == 503
    assert response.json()["error"] == "payments_unavailable"


def test_metrics_route_needs_a_session_factory_dependency():
    assert get_session_factory.__name__ == "get_session_factory"