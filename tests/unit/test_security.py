from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import Depends, FastAPI
from pydantic import ValidationError
from starlette.requests import Request

from backend.api import security
from backend.api.deps import (
    ApiError,
    current_owner,
    get_api,
    install_error_handlers,
    translate_errors,
)
from backend.api.passport import make_token, read_token
from backend.api.routes_payments import router as payments_router
from backend.api.security import (
    BodySizeLimitMiddleware,
    RateLimiter,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    make_session,
    read_session,
)
from backend.config.observability import scrub_event
from backend.config.settings import Settings, get_settings
from backend.intelligence.masking import mask_text
from backend.payments.paypal_client import PayPalOutcomeUnknown
from backend.webhooks import receiver
from backend.webhooks.events_map import mask_payload
from backend.webhooks.handler import REJECTED, VERIFICATION_UNAVAILABLE
from scripts import scan_secrets

SECRET = "s" * 12 + "K" * 12 + "9" * 12
OTHER_SECRET = "o" * 12 + "T" * 12 + "3" * 12
ADMIN = "a" * 12 + "D" * 12 + "5" * 12
EMAIL_LIKE = re.compile(r"[^@\s\"']+@[^@\s\"']+\.[^@\s\"']+")


def use_env(monkeypatch, **values):
    base = {
        "ENV": "local",
        "PASSPORT_SIGNING_SECRET": SECRET,
        "ADMIN_API_KEY": ADMIN,
        "FRONTEND_ORIGIN": "http://localhost:3000",
    }
    base.update(values)
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    for name, value in base.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    use_env(monkeypatch)
    yield
    get_settings.cache_clear()


def http(app, *, base="http://test", client=("203.0.113.5", 5000)):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url=base)


# ------------------------------------------------------------------ 1. signed sessions
def test_a_session_round_trips_to_its_owner():
    token = make_session("demo-alice", SECRET)
    assert read_session(token, SECRET) == "demo-alice"


def test_an_expired_session_is_rejected():
    token = make_session("demo-alice", SECRET, ttl_seconds=60, now=1_000_000)
    assert read_session(token, SECRET, now=1_000_030) == "demo-alice"
    assert read_session(token, SECRET, now=1_000_061) is None


def test_a_session_signed_with_another_secret_is_rejected():
    assert read_session(make_session("demo-alice", OTHER_SECRET), SECRET) is None


@pytest.mark.parametrize("position", [0, 5, -5, -1])
def test_a_tampered_session_is_rejected(position):
    token = make_session("demo-alice", SECRET)
    chars = list(token)
    chars[position] = "A" if chars[position] != "A" else "B"
    assert read_session("".join(chars), SECRET) is None


def test_changing_the_owner_inside_a_session_is_rejected():
    import base64

    token = make_session("demo-alice", SECRET)
    _, _, mac = token.partition(".")
    forged_payload = base64.urlsafe_b64encode(b"demo-bob:99999999999").rstrip(b"=").decode()
    assert read_session(f"{forged_payload}.{mac}", SECRET) is None


@pytest.mark.parametrize(
    "value", [None, "", ".", "abc", "a.b.c", "é.é", "demo-alice", "x" * 5000, "a." + "b" * 43]
)
def test_garbage_is_never_a_session_and_never_raises(value):
    assert read_session(value, SECRET) is None


def test_a_session_needs_a_secret():
    with pytest.raises(ValueError):
        make_session("demo-alice", "")
    assert read_session(make_session("demo-alice", SECRET), "") is None


def test_a_passport_share_token_is_not_a_session_and_the_reverse():
    share, _ = make_token("11111111-1111-1111-1111-111111111111", SECRET, datetime.now(UTC))
    assert read_session(share, SECRET) is None
    assert read_token(make_session("demo-alice", SECRET), SECRET, datetime.now(UTC)) is None


def owner_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(security.router)

    @app.get("/whoami")
    async def whoami(owner: str = Depends(current_owner)) -> dict:
        return {"owner": owner}

    return app


async def test_demo_login_issues_an_http_only_lax_cookie_that_identifies_the_owner():
    async with http(owner_app()) as client:
        login = await client.post("/api/demo-login", json={"user": "Alice"})
        assert login.status_code == 200
        assert login.json()["owner_id"] == "demo-alice"
        header = login.headers["set-cookie"].lower()
        assert "httponly" in header and "samesite=lax" in header and "path=/" in header
        assert "secure" not in header  # ENV=local runs over plain http
        assert SECRET not in login.text and "slothpay_owner=" not in login.text
        assert (await client.get("/whoami")).json() == {"owner": "demo-alice"}


async def test_the_cookie_is_secure_outside_local(monkeypatch):
    use_env(monkeypatch, ENV="demo")
    async with http(owner_app(), base="https://test") as client:
        login = await client.post("/api/demo-login", json={"user": "bob"})
        assert "secure" in login.headers["set-cookie"].lower()
        assert (await client.get("/whoami")).json() == {"owner": "demo-bob"}


async def test_a_guest_login_gives_each_visitor_a_private_identity():
    async with http(owner_app()) as one, http(owner_app()) as two:
        first = (await one.post("/api/demo-login")).json()["owner_id"]
        second = (await two.post("/api/demo-login")).json()["owner_id"]
    assert first != second and first.startswith("demo-")


async def test_an_unknown_demo_user_is_refused():
    async with http(owner_app()) as client:
        refused = await client.post("/api/demo-login", json={"user": "mallory"})
        assert refused.status_code == 400
        assert "set-cookie" not in refused.headers
        assert (await client.get("/whoami")).status_code == 401


async def test_login_is_unavailable_without_a_signing_secret(monkeypatch):
    use_env(monkeypatch, PASSPORT_SIGNING_SECRET="")
    async with http(owner_app()) as client:
        response = await client.post("/api/demo-login", json={"user": "alice"})
    assert response.status_code == 503 and "set-cookie" not in response.headers


async def test_logout_clears_the_cookie():
    async with http(owner_app()) as client:
        await client.post("/api/demo-login", json={"user": "alice"})
        out = await client.post("/api/demo-logout")
        assert out.status_code == 200
        assert (await client.get("/whoami")).status_code == 401


async def test_a_tampered_cookie_is_401():
    good = make_session("demo-alice", SECRET)
    bad = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    async with http(owner_app()) as client:
        response = await client.get("/whoami", headers={"Cookie": f"slothpay_owner={bad}"})
    assert response.status_code == 401


async def test_the_owner_header_works_locally_but_is_ignored_in_demo(monkeypatch):
    async with http(owner_app()) as client:
        assert (await client.get("/whoami", headers={"X-Owner-Id": "dev-1"})).status_code == 200
    use_env(monkeypatch, ENV="demo")
    async with http(owner_app()) as client:
        assert (await client.get("/whoami", headers={"X-Owner-Id": "dev-1"})).status_code == 401


class OwnedApi:
    """Knows one payment and who owns it. Everyone else sees nothing, like the real service."""

    def __init__(self, owner: str, payment_id: str) -> None:
        self.owner, self.payment_id = owner, payment_id

    async def get_payment(self, owner_id, payment_id):
        return None  # reached only for the wrong owner in this test

    async def list_payments(self, owner_id, limit, cursor):
        return [], None


async def test_another_owner_gets_404_exactly_like_a_missing_payment():
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(security.router)
    app.include_router(payments_router)
    app.state.payment_api = OwnedApi("demo-alice", str(uuid.uuid4()))
    app.dependency_overrides[get_api] = lambda: app.state.payment_api
    async with http(app) as bob:
        await bob.post("/api/demo-login", json={"user": "bob"})
        theirs = await bob.get(f"/api/payments/{app.state.payment_api.payment_id}")
        missing = await bob.get(f"/api/payments/{uuid.uuid4()}")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()


# ------------------------------------------------------------------ 2. rate limits
def limited_app(redis, limit=3, *, rules=None):
    app = FastAPI()

    @app.post("/api/payments")
    async def create() -> dict:
        return {"ok": True}

    @app.get("/api/payments")
    async def listing() -> dict:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        redis_factory=lambda: redis,
        rules=rules or {("POST", "/api/payments"): ("payments", limit)},
    )
    return app


async def test_the_rate_limit_returns_429_with_retry_after():
    redis = FakeAsyncRedis(decode_responses=True)
    async with http(limited_app(redis, 3)) as client:
        codes = [(await client.post("/api/payments")).status_code for _ in range(5)]
        blocked = await client.post("/api/payments")
    assert codes == [200, 200, 200, 429, 429]
    assert blocked.json()["error"] == "rate_limited"
    assert 1 <= int(blocked.headers["retry-after"]) <= 60
    assert blocked.json()["retry_after"] == int(blocked.headers["retry-after"])


async def test_each_client_has_its_own_budget_and_reads_are_not_limited():
    redis = FakeAsyncRedis(decode_responses=True)
    app = limited_app(redis, 1)
    async with (
        http(app, client=("198.51.100.1", 1)) as one,
        http(app, client=("198.51.100.2", 1)) as two,
    ):
        assert (await one.post("/api/payments")).status_code == 200
        assert (await one.post("/api/payments")).status_code == 429
        assert (await two.post("/api/payments")).status_code == 200
        for _ in range(10):
            assert (await one.get("/api/payments")).status_code == 200


async def test_the_budget_comes_back_after_the_window():
    redis = FakeAsyncRedis(decode_responses=True)
    limiter = RateLimiter(lambda: redis)
    assert await limiter.hit("x", "c", 1, 60) is None
    assert await limiter.hit("x", "c", 1, 60) is not None
    await redis.delete("slothpay:rl:x:c")  # what the expiry does
    assert await limiter.hit("x", "c", 1, 60) is None


async def test_a_counter_without_an_expiry_still_expires():
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("slothpay:rl:x:c", 99)  # no TTL: it would block forever
    wait = await RateLimiter(lambda: redis).hit("x", "c", 3, 60)
    assert wait == 60 and await redis.ttl("slothpay:rl:x:c") > 0


async def test_a_limit_of_zero_switches_the_rule_off():
    redis = FakeAsyncRedis(decode_responses=True)
    async with http(limited_app(redis, 0)) as client:
        codes = [(await client.post("/api/payments")).status_code for _ in range(5)]
    assert codes == [200] * 5


async def test_if_redis_is_down_requests_are_allowed_not_blocked():
    def broken():
        raise ConnectionError("redis is down")

    app = FastAPI()

    @app.post("/api/payments")
    async def create() -> dict:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        redis_factory=broken,
        rules={("POST", "/api/payments"): ("payments", 1)},
    )
    async with http(app) as client:
        assert [(await client.post("/api/payments")).status_code for _ in range(3)] == [200] * 3


def test_the_default_rules_cover_payments_webhook_and_login():
    rules = security.rate_rules(get_settings())
    assert {key for key in rules} == {
        ("POST", "/api/payments"),
        ("POST", "/webhooks/paypal"),
        ("POST", "/api/demo-login"),
    }
    assert all(limit > 0 for _, limit in rules.values())


# ------------------------------------------------------------------ 3. request size
def sized_app(max_bytes=100, overrides=None):
    app = FastAPI()

    @app.post("/read")
    async def read(request: Request) -> dict:
        return {"size": len(await request.body())}

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes, overrides=overrides)
    return app


async def test_a_body_over_the_cap_is_413_by_its_declared_length():
    async with http(sized_app()) as client:
        big = await client.post("/read", content=b"x" * 101)
        ok = await client.post("/read", content=b"x" * 100)
    assert big.status_code == 413 and big.json()["error"] == "payload_too_large"
    assert ok.status_code == 200 and ok.json() == {"size": 100}


async def test_a_streamed_body_with_no_length_is_cut_off_too():
    async def chunks():
        for _ in range(10):
            yield b"y" * 30

    async with http(sized_app()) as client:
        response = await client.post("/read", content=chunks())
    assert response.status_code == 413


async def test_a_route_can_have_its_own_larger_cap_and_gets_stay_open():
    app = sized_app(100, overrides={"/read": 1000})
    async with http(app) as client:
        assert (await client.post("/read", content=b"x" * 900)).status_code == 200
        assert (await client.post("/read", content=b"x" * 1001)).status_code == 413
        assert (await client.get("/ping")).status_code == 200


# ------------------------------------------------------------------ 4. headers and CORS
def headers_app(hsts=False):
    app = FastAPI()

    @app.get("/api/thing")
    async def thing() -> dict:
        return {"ok": True}

    @app.get("/docs")
    async def docs() -> dict:
        return {"ok": True}

    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    return app


async def test_security_headers_are_on_every_response_including_errors():
    async with http(headers_app()) as client:
        for path in ("/api/thing", "/missing"):
            headers = (await client.get(path)).headers
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["x-frame-options"] == "DENY"
            assert headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in headers["content-security-policy"]
            assert "camera=()" in headers["permissions-policy"]
        assert (await client.get("/api/thing")).headers["cache-control"] == "no-store"


async def test_hsts_only_when_asked_and_docs_keep_a_workable_policy():
    async with http(headers_app(hsts=False)) as client:
        assert "strict-transport-security" not in (await client.get("/api/thing")).headers
    async with http(headers_app(hsts=True)) as client:
        assert "max-age" in (await client.get("/api/thing")).headers["strict-transport-security"]
        assert "content-security-policy" not in (await client.get("/docs")).headers


async def test_limited_and_oversized_responses_also_carry_the_headers():
    from backend.main import create_app

    app = create_app()
    async with http(app) as client:
        big = await client.post("/api/payments", content=b"z" * 70_000)
    assert big.status_code == 413
    assert big.headers["x-frame-options"] == "DENY"


async def test_cors_allows_only_the_frontend_origin_and_never_a_wildcard(monkeypatch):
    use_env(monkeypatch, FRONTEND_ORIGIN="https://app.example.test")
    from backend.main import create_app

    app = create_app()
    preflight = {"Access-Control-Request-Method": "POST"}
    async with http(app) as client:
        good = await client.options(
            "/api/payments", headers={"Origin": "https://app.example.test", **preflight}
        )
        evil = await client.options(
            "/api/payments", headers={"Origin": "https://evil.example", **preflight}
        )
        simple = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert good.headers["access-control-allow-origin"] == "https://app.example.test"
    assert good.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in evil.headers
    assert "access-control-allow-origin" not in simple.headers
    for response in (good, evil, simple):
        assert response.headers.get("access-control-allow-origin") != "*"


# ------------------------------------------------------------------ 5. startup refusals
def make(**kwargs):
    base = {"ENV": "demo", "ADMIN_API_KEY": ADMIN, "PASSPORT_SIGNING_SECRET": SECRET}
    base.update(kwargs)
    return Settings(_env_file=None, **base)


def test_startup_refuses_a_live_paypal_url_in_demo_mode(monkeypatch):
    use_env(monkeypatch, ENV="demo", PAYPAL_BASE_URL="https://api-m.paypal.com")
    from backend.main import create_app

    with pytest.raises(ValidationError, match="not the PayPal sandbox"):
        create_app()


@pytest.mark.parametrize(
    "url",
    [
        "https://api-m.paypal.com",
        "https://evil.example/sandbox",
        "https://sandbox.paypal.com.evil.example",
        "https://notsandbox.paypal.com",
        "http://api-m.sandbox.paypal.com",
        "https://evil.example/?x=.sandbox.paypal.com",
        "",
    ],
)
@pytest.mark.parametrize("env", ["demo", "production"])
def test_only_the_real_sandbox_host_is_accepted_in_demo_and_production(env, url):
    with pytest.raises(ValidationError):
        make(ENV=env, PAYPAL_BASE_URL=url)


def test_the_real_sandbox_hosts_are_accepted():
    for url in ("https://api-m.sandbox.paypal.com", "https://api.sandbox.paypal.com"):
        assert make(PAYPAL_BASE_URL=url).ENV == "demo"


@pytest.mark.parametrize("name", ["ADMIN_API_KEY", "PASSPORT_SIGNING_SECRET"])
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "short",
        "x" * 31,
        "change-me-to-a-long-random-string-at-least-32-chars",
        "your-" + "k" * 40,
    ],
)
@pytest.mark.parametrize("env", ["demo", "production"])
def test_weak_secrets_stop_the_app_in_demo_and_production(env, name, bad):
    with pytest.raises(ValidationError, match=name):
        make(ENV=env, **{name: bad})


def test_secrets_of_32_characters_are_enough():
    assert make(ADMIN_API_KEY="k" * 16 + "Z" * 16, PASSPORT_SIGNING_SECRET=SECRET).ENV == "demo"


def test_empty_secrets_are_fine_locally(monkeypatch):
    use_env(monkeypatch, ADMIN_API_KEY="", PASSPORT_SIGNING_SECRET="")
    assert Settings(_env_file=None, ENV="local").ADMIN_API_KEY == ""


@pytest.mark.parametrize("env", ["demo", "production"])
def test_chaos_stops_the_app_outside_local_and_sandbox(env):
    with pytest.raises(ValidationError, match="CHAOS_ENABLED"):
        make(ENV=env, CHAOS_ENABLED=True)
    for ok in ("local", "sandbox"):
        assert Settings(_env_file=None, ENV=ok, CHAOS_ENABLED=True).CHAOS_ENABLED is True


def test_an_unknown_environment_name_is_refused():
    with pytest.raises(ValidationError, match="ENV must be"):
        Settings(_env_file=None, ENV="staging")


@pytest.mark.parametrize(
    "overrides", [{"SESSION_TTL_HOURS": 0}, {"SESSION_TTL_HOURS": 9999}, {"MAX_REQUEST_BYTES": 10}]
)
def test_silly_session_and_size_limits_are_refused(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


# ------------------------------------------------------------------ 5b. the secrets scan
def fake(*parts):
    """Built at run time so this file itself never contains something the scan would flag."""
    return "".join(parts)


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("OpenAI-style API key", fake("key = '", "sk-", "A1b2C3d4E5f6G7h8I9j0K1l2", "'")),
        (
            "PayPal client secret",
            fake(
                "PAYPAL_CLIENT_",
                "SECRET=",
                "EGnHDxD_qRPdaLdZz8iCr8N7_MzF-YHPTkjs6NKYQvQSBngp4PTTVWkPZRbL",
            ),
        ),
        ("bearer token", fake("Authorization: Bea", "rer ", "kJ8s9dKs82hdK29dJs8d2JKs82hs9dK")),
        ("private key block", fake("-----BEGIN RSA ", "PRIVATE KEY-----")),
        ("AWS access key id", fake("AK", "IA", "ABCDEFGHIJKLMNOP")),
        ("GitHub token", fake("gh", "p_", "A" * 36)),
        ("Google API key", fake("AI", "za", "S" * 35)),
        ("PayPal access token", fake("A21", "AA", "x" * 50)),
        ("secret-looking assignment", fake('api_key = "', "Zk39dKs82hdK29dJs8d2JKs8", '"')),
    ],
)
def test_the_scan_finds_a_planted_fake_key(tmp_path, capsys, label, line):
    target = tmp_path / "planted.py"
    target.write_text(f"x = 1\n{line}\n")
    assert scan_secrets.main([str(target)]) == 1
    out = capsys.readouterr().out
    assert "planted.py:2" in out and label in out
    secret_part = line.split("=")[-1].strip(" '\"") if "=" in line else ""
    assert not secret_part or secret_part not in out  # a hit never prints the secret itself


def test_the_scan_is_clean_for_normal_code_placeholders_and_marked_lines(tmp_path, capsys):
    (tmp_path / "ok.py").write_text(
        "PAYPAL_CLIENT_SECRET=your-sandbox-client-secret\n"
        "ADMIN_API_KEY=change-me-to-a-long-random-string-at-least-32-chars\n"
        "password = 'short'\n"
        f"token = '{fake('sk-', 'A1b2C3d4E5f6G7h8I9j0K1l2')}'  # scan:allow\n"
        "headers = {'Authorization': f'Bearer {token}'}\n"
    )
    assert scan_secrets.main([str(tmp_path)]) == 0
    assert "clean" in capsys.readouterr().out


def test_the_scan_skips_env_example_node_modules_and_binaries(tmp_path):
    secret_line = fake("key = '", "sk-", "A1b2C3d4E5f6G7h8I9j0K1l2", "'\n")
    (tmp_path / ".env.example").write_text(secret_line)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text(secret_line)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01" + secret_line.encode())
    assert scan_secrets.main([str(tmp_path)]) == 0
    (tmp_path / ".env").write_text(secret_line)  # a real .env is scanned
    assert scan_secrets.main([str(tmp_path)]) == 1


def test_the_scan_reports_a_missing_path_with_exit_2(tmp_path, capsys):
    assert scan_secrets.main([str(tmp_path / "nope")]) == 2


def test_the_repository_itself_is_clean(capsys):
    assert (
        scan_secrets.main(
            [
                str(scan_secrets.ROOT / d)
                for d in ("backend", "tests", "scripts", "frontend/src", "database")
            ]
        )
        == 0
    )


def test_the_repository_gitignore_covers_env_files():
    lines = (scan_secrets.ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines and ".env.*" in lines and "!.env.example" in lines


# ------------------------------------------------------------------ 6. privacy
PAYPAL_WEBHOOK = {
    "id": "WH-1",
    "event_type": "PAYMENT.CAPTURE.COMPLETED",
    "resource": {
        "id": "CAP-1",
        "amount": {"value": "10.00", "currency_code": "USD"},
        "payer": {
            "email_address": "jane.doe@example.com",
            "payer_id": "PAYERID123",
            "name": {"given_name": "Jane", "surname": "Doe"},
            "address": {"address_line_1": "1 Privet Drive", "postal_code": "90210"},
            "phone": {"phone_number": {"national_number": "5551234567"}},
        },
        "payee": {"email_address": "merchant@shop.example", "merchant_id": "M123"},
        "note_to_payer": "Questions? write to billing.team@shop.example today",
        "supplementary_data": {"related_ids": {"order_id": "ORDER-9"}},
        "links": [{"href": "https://api.sandbox.paypal.com/v2/payments/captures/CAP-1"}],
    },
    "shipping": {
        "name": {"full_name": "Jane Doe"},
        "address": {"address_line_1": "1 Privet Drive"},
    },
    "purchase_units": [
        {
            "shipping": {"address": {"admin_area_2": "Springfield"}},
            "payer": {"email_address": "second.buyer@example.org"},
            "description": "contact me: someone+tag@sub.mail.example.co.uk",
        }
    ],
}
PII_VALUES = [
    "Jane",
    "Doe",
    "1 Privet Drive",
    "90210",
    "5551234567",
    "Springfield",
    "Jane Doe",
]


def test_masking_a_paypal_payload_leaves_no_email_name_or_address():
    masked = mask_payload(PAYPAL_WEBHOOK)
    text = json.dumps(masked)
    assert not EMAIL_LIKE.search(text), EMAIL_LIKE.findall(text)
    for value in PII_VALUES:
        assert value not in text, value
    # what the app needs is kept
    assert masked["resource"]["amount"]["value"] == "10.00"
    assert masked["resource"]["supplementary_data"]["related_ids"]["order_id"] == "ORDER-9"
    assert masked["event_type"] == "PAYMENT.CAPTURE.COMPLETED"


def test_masking_does_not_change_the_original_payload():
    before = json.dumps(PAYPAL_WEBHOOK, sort_keys=True)
    mask_payload(PAYPAL_WEBHOOK)
    assert json.dumps(PAYPAL_WEBHOOK, sort_keys=True) == before


@pytest.mark.parametrize(
    "text",
    [
        "Pay jane.doe@example.com now",
        "a@b.co and c.d+e@f-g.example.org",
        "UPPER.CASE@EXAMPLE.COM",
    ],
)
def test_free_text_masking_removes_emails(text):
    assert not EMAIL_LIKE.search(mask_text(text))


def test_sentry_events_are_scrubbed_of_emails_tokens_cookies_and_bodies():
    bearer = fake("Bea", "rer ", "kJ8s9dKs82hdK29dJs8d2JKs82hs9dK")
    event = {
        "message": "capture failed for jane.doe@example.com",
        "exception": {"values": [{"type": "X", "value": "payer billing@shop.example refused"}]},
        "request": {
            "headers": {"Authorization": bearer, "Cookie": "slothpay_owner=abc", "Accept": "*/*"},
            "data": {"payer": {"email_address": "jane.doe@example.com"}},
            "cookies": {"slothpay_owner": "abc"},
            "query_string": "email=jane.doe@example.com",
        },
        "user": {"email": "jane.doe@example.com", "id": "demo-alice"},
        "extra": {"note": f"sent {bearer} to host", "owner": "demo-alice"},
    }
    clean = scrub_event(event)
    text = json.dumps(clean)
    assert not EMAIL_LIKE.search(text), EMAIL_LIKE.findall(text)
    assert "kJ8s9dKs82hdK29dJs8d2JKs82hs9dK" not in text and "slothpay_owner=abc" not in text
    assert "user" not in clean and "data" not in clean["request"]
    assert clean["request"]["headers"]["Accept"] == "*/*"


def test_logs_for_a_failed_paypal_call_carry_no_raw_error_text(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ApiError) as caught, translate_errors():
        raise PayPalOutcomeUnknown("buyer jane.doe@example.com card 4111111111111111")
    assert caught.value.status_code == 503
    assert "jane.doe" not in caplog.text and "4111" not in caplog.text
    assert "jane.doe" not in caught.value.message


# ------------------------------------------------------------------ 7. webhooks
def webhook_app(monkeypatch, behaviour):
    seen = {}

    async def fake_process(headers, raw_body, deps):
        seen["raw"] = raw_body
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour

    monkeypatch.setattr(receiver, "process_webhook", fake_process)
    app = FastAPI()
    app.include_router(receiver.router)
    app.dependency_overrides[receiver.get_webhook_deps] = lambda: object()
    return app, seen


async def test_the_webhook_verifies_the_exact_raw_bytes(monkeypatch):
    app, seen = webhook_app(monkeypatch, "applied")
    raw = b'{"id":"WH-1",  "spaced" :  true}'
    async with http(app) as client:
        response = await client.post("/webhooks/paypal", content=raw)
    assert response.status_code == 200 and seen["raw"] == raw


@pytest.mark.parametrize(
    ("behaviour", "status", "text"),
    [
        (REJECTED, 401, "Webhook signature rejected"),
        (VERIFICATION_UNAVAILABLE, 503, "Webhook verification unavailable"),
        (RuntimeError("db password=hunter2 host=10.0.0.5"), 503, "Webhook processing unavailable"),
    ],
)
async def test_webhook_failures_return_fixed_text_never_the_reason(
    monkeypatch, behaviour, status, text
):
    app, _ = webhook_app(monkeypatch, behaviour)
    async with http(app) as client:
        response = await client.post("/webhooks/paypal", content=b"{}")
    assert response.status_code == status
    assert response.json() == {"detail": text}
    assert "hunter2" not in response.text and "10.0.0.5" not in response.text


async def test_the_webhook_route_refuses_an_oversized_body(monkeypatch):
    app, _ = webhook_app(monkeypatch, "applied")
    async with http(app) as client:
        response = await client.post("/webhooks/paypal", content=b"x" * (256 * 1024 + 1))
    assert response.status_code == 413


async def test_the_metrics_route_needs_a_session():
    from backend.api.routes_metrics import router as metrics_router

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(metrics_router)
    async with http(app) as client:
        anonymous = await client.get("/api/metrics")
        with_session = await client.get("/api/metrics", headers={"X-Owner-Id": "viewer-1"})
    assert anonymous.status_code == 401
    assert with_session.status_code == 503  # signed in, but this app has no database wired