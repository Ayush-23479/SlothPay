"""Unit tests for duplicate protection: fake repository plus fakeredis, no real database."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import fakeredis
import pytest
from sqlalchemy.exc import IntegrityError

from backend.models.payment import Payment
from backend.reliability import idempotency
from backend.reliability.idempotency import (
    FINAL_STATUSES,
    PATHS,
    REQUEST_ID_NAMESPACE,
    IntentBusy,
    PayloadMismatch,
    derive_request_id,
    intent_lock,
    lock_name,
    register_intent,
    similar_open_intent,
)

OWNER = "buyer-1"
KEY = "key-0000-0001"


def payload(**overrides):
    base = {"path": "SMARTPAY", "amount": "500.00", "currency": "USD", "description": "Rs 500"}
    base.update(overrides)
    return base


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeRepo:
    """In-memory stand-in for the repository functions idempotency.py calls."""

    def __init__(self):
        self.intents: dict[tuple[str, str], dict] = {}
        self.audit: list[dict] = []
        self.create_calls = 0
        self.competitor: dict | None = None  # row another process "inserts first"

    def add_row(self, owner=OWNER, key="other-key-0001", amount="500.00", currency="USD",
                path="SMARTPAY", status="CREATED", age_seconds=0, order_id=None):
        row = {
            "id": str(uuid.uuid4()),
            "owner_id": owner,
            "idempotency_key": key,
            "merchant_order_id": order_id or key,
            "path": path,
            "amount": Decimal(amount),
            "currency": currency,
            "status": status,
            "version": 0,
            "created_at": datetime.now(UTC) - timedelta(seconds=age_seconds),
        }
        self.intents[(owner, key)] = row
        return row

    async def get_intent_by_key(self, session, owner_id, idempotency_key):
        await asyncio.sleep(0)
        row = self.intents.get((owner_id, idempotency_key))
        return dict(row) if row else None

    async def get_intent_by_order_id(self, session, owner_id, merchant_order_id):
        await asyncio.sleep(0)
        for row in self.intents.values():
            if row["owner_id"] == owner_id and row["merchant_order_id"] == merchant_order_id:
                return dict(row)
        return None

    async def get_intent_created_evidence(self, session, payment_id):
        for event in self.audit:
            if event["payment_id"] == payment_id and event["event_type"] == "INTENT_CREATED":
                return event["evidence"]
        return None

    async def create_intent(self, session, **kwargs):
        await asyncio.sleep(0)
        self.create_calls += 1
        if self.competitor is not None:
            rival = self.competitor
            self.intents[(rival["owner_id"], rival["idempotency_key"])] = rival
            self.competitor = None
        for row in self.intents.values():  # the two UNIQUE constraints
            if row["owner_id"] == kwargs["owner_id"] and (
                row["idempotency_key"] == kwargs["idempotency_key"]
                or row["merchant_order_id"] == kwargs["merchant_order_id"]
            ):
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        row = self.add_row(
            owner=kwargs["owner_id"],
            key=kwargs["idempotency_key"],
            amount=str(kwargs["amount"]),
            currency=kwargs["currency"],
            path=kwargs["path"],
            order_id=kwargs["merchant_order_id"],
        )
        return dict(row)

    async def find_recent_intents(self, session, *, owner_id, amount, currency, since, limit=20):
        # Deliberately ignores `since`: idempotency.py must apply the window itself.
        rows = [
            dict(r)
            for r in self.intents.values()
            if r["owner_id"] == owner_id and r["amount"] == amount and r["currency"] == currency
        ]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    async def add_audit_event(self, session, *, payment_id, event_type, actor, evidence,
                              from_state=None, to_state=None):
        self.audit.append({"payment_id": payment_id, "event_type": event_type,
                           "actor": actor, "evidence": evidence, "to_state": to_state})

    def events(self, event_type):
        return [e for e in self.audit if e["event_type"] == event_type]


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(idempotency, "get_intent_by_key", fake.get_intent_by_key)
    monkeypatch.setattr(idempotency, "get_intent_by_order_id", fake.get_intent_by_order_id)
    monkeypatch.setattr(
        idempotency, "get_intent_created_evidence", fake.get_intent_created_evidence
    )
    monkeypatch.setattr(idempotency, "create_intent", fake.create_intent)
    monkeypatch.setattr(idempotency, "find_recent_intents", fake.find_recent_intents)
    monkeypatch.setattr(idempotency, "add_audit_event", fake.add_audit_event)
    return fake


@pytest.fixture(params=[True, False], ids=["decoded", "bytes"])
async def redis(request):
    client = fakeredis.FakeAsyncRedis(decode_responses=request.param)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def session():
    return FakeSession()


# --- Layer 2: request id -----------------------------------------------------------------


def test_derive_request_id_is_stable_and_operation_specific():
    first = derive_request_id("intent-1", "create_order")
    assert first == derive_request_id("intent-1", "create_order")
    assert first == str(uuid.uuid5(REQUEST_ID_NAMESPACE, "intent-1:create_order"))
    assert uuid.UUID(first)  # a valid uuid string, safe as PayPal-Request-Id
    assert first != derive_request_id("intent-1", "capture")
    assert first != derive_request_id("intent-2", "create_order")


# --- Layer 1: register_intent ------------------------------------------------------------


async def test_first_register_creates_one_intent_and_audits_it(repo, redis, session):
    intent, replay = await register_intent(session, redis, OWNER, KEY, payload())
    assert replay is False
    assert intent["amount"] == Decimal("500.00")
    assert intent["status"] == "CREATED"
    assert len(repo.intents) == 1
    created = repo.events("INTENT_CREATED")
    assert len(created) == 1
    assert created[0]["evidence"]["source"] == "api"
    assert created[0]["evidence"]["confirmed"] is False
    assert created[0]["payment_id"] == intent["id"]
    assert session.commits == 1


async def test_without_an_order_id_the_key_doubles_as_the_order_id(repo, redis, session):
    intent, _ = await register_intent(session, redis, OWNER, KEY, payload())
    assert intent["merchant_order_id"] == KEY


async def test_same_key_returns_same_intent_with_replay_true(repo, redis, session):
    first, first_replay = await register_intent(session, redis, OWNER, KEY, payload())
    second, second_replay = await register_intent(session, redis, OWNER, KEY, payload())
    assert (first_replay, second_replay) == (False, True)
    assert second["id"] == first["id"]
    assert len(repo.intents) == 1
    assert repo.create_calls == 1
    suppressed = repo.events("DUPLICATE_SUPPRESSED")
    assert len(suppressed) == 1
    assert suppressed[0]["evidence"]["reason"] == "same_key"


async def test_replay_accepts_the_same_amount_written_differently(repo, redis, session):
    first, _ = await register_intent(session, redis, OWNER, KEY, payload(amount="500"))
    second, replay = await register_intent(session, redis, OWNER, KEY, payload(amount="500.00"))
    assert replay is True and second["id"] == first["id"]


@pytest.mark.parametrize(
    "changes",
    [{"amount": "501.00"}, {"currency": "EUR"}, {"path": "CONDITIONALPAY"}],
    ids=["amount", "currency", "path"],
)
async def test_same_key_with_different_values_raises_payload_mismatch(
    repo, redis, session, changes
):
    await register_intent(session, redis, OWNER, KEY, payload())
    with pytest.raises(PayloadMismatch):
        await register_intent(session, redis, OWNER, KEY, payload(**changes))
    assert len(repo.intents) == 1
    assert session.rollbacks == 1


async def test_short_path_names_are_accepted_as_aliases(repo, redis, session):
    intent, _ = await register_intent(session, redis, OWNER, KEY, payload(path="smart"))
    assert intent["path"] == "SMARTPAY"
    other, _ = await register_intent(
        session, redis, OWNER, "key-0000-0002", payload(path="CONDITIONAL", amount="9.99")
    )
    assert other["path"] == "CONDITIONALPAY"


async def test_two_concurrent_calls_with_the_same_key_leave_exactly_one_intent(
    repo, redis
):
    results = await asyncio.gather(
        register_intent(FakeSession(), redis, OWNER, KEY, payload()),
        register_intent(FakeSession(), redis, OWNER, KEY, payload()),
        return_exceptions=True,
    )
    assert len(repo.intents) == 1
    assert repo.create_calls == 1
    created = [r for r in results if not isinstance(r, Exception) and r[1] is False]
    assert len(created) == 1
    for outcome in results:
        assert isinstance(outcome, IntentBusy) or outcome[0]["id"] == created[0][0]["id"]


async def test_unique_violation_on_insert_is_treated_as_a_replay(repo, redis, session):
    rival = repo.add_row(key=KEY)
    del repo.intents[(OWNER, KEY)]
    repo.competitor = rival  # appears only when our insert runs, like a racing process
    intent, replay = await register_intent(session, redis, OWNER, KEY, payload())
    assert replay is True
    assert intent["id"] == rival["id"]
    assert repo.events("INTENT_CREATED") == []
    assert repo.events("DUPLICATE_SUPPRESSED")[0]["evidence"]["reason"] == "same_key"


async def test_unique_violation_with_different_values_raises_payload_mismatch(
    repo, redis, session
):
    rival = repo.add_row(key=KEY, amount="999.00")
    del repo.intents[(OWNER, KEY)]
    repo.competitor = rival
    with pytest.raises(PayloadMismatch):
        await register_intent(session, redis, OWNER, KEY, payload())


async def test_unique_violation_without_a_row_to_read_back_is_reraised(
    repo, redis, session, monkeypatch
):
    async def always_conflict(session, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("some other constraint"))

    monkeypatch.setattr(idempotency, "create_intent", always_conflict)
    with pytest.raises(IntegrityError):
        await register_intent(session, redis, OWNER, KEY, payload())


# --- same key, conflicting order id or condition (must be rejected) ----------------------

CONDITION = {"kind": "manual", "description": "Parcel delivered"}


async def test_same_key_with_a_different_order_id_raises_payload_mismatch(repo, redis, session):
    await register_intent(session, redis, OWNER, KEY, payload(merchant_order_id="order-A"))
    with pytest.raises(PayloadMismatch):
        await register_intent(session, redis, OWNER, KEY, payload(merchant_order_id="order-B"))
    assert len(repo.intents) == 1


async def test_same_key_with_an_order_id_added_or_dropped_raises_payload_mismatch(
    repo, redis, session
):
    await register_intent(session, redis, OWNER, KEY, payload(merchant_order_id="order-A"))
    with pytest.raises(PayloadMismatch):  # replay forgets the order id
        await register_intent(session, redis, OWNER, KEY, payload())
    other = "key-0000-0002"
    await register_intent(session, redis, OWNER, other, payload(amount="7.00"))
    with pytest.raises(PayloadMismatch):  # replay adds an order id
        await register_intent(
            session, redis, OWNER, other, payload(amount="7.00", merchant_order_id="order-A")
        )


async def test_same_key_with_the_same_order_id_is_a_replay(repo, redis, session):
    first, _ = await register_intent(
        session, redis, OWNER, KEY, payload(merchant_order_id="order-A")
    )
    again, replay = await register_intent(
        session, redis, OWNER, KEY, payload(merchant_order_id="order-A")
    )
    assert replay is True and again["id"] == first["id"]
    assert first["merchant_order_id"] == "order-A"


@pytest.mark.parametrize(
    "replay_condition",
    [
        {"kind": "manual", "description": "Something else"},
        {"kind": "manual"},
        None,
    ],
    ids=["changed", "reduced", "dropped"],
)
async def test_same_key_with_a_different_condition_raises_payload_mismatch(
    repo, redis, session, replay_condition
):
    base = payload(path="CONDITIONALPAY", condition=CONDITION)
    await register_intent(session, redis, OWNER, KEY, base)
    with pytest.raises(PayloadMismatch):
        await register_intent(
            session, redis, OWNER, KEY, payload(path="CONDITIONALPAY", condition=replay_condition)
        )
    assert len(repo.intents) == 1


async def test_same_key_with_a_condition_added_raises_payload_mismatch(repo, redis, session):
    await register_intent(session, redis, OWNER, KEY, payload(path="CONDITIONALPAY"))
    with pytest.raises(PayloadMismatch):
        await register_intent(
            session, redis, OWNER, KEY, payload(path="CONDITIONALPAY", condition=CONDITION)
        )


async def test_same_key_with_the_same_condition_is_a_replay_whatever_the_key_order(
    repo, redis, session
):
    first, _ = await register_intent(
        session, redis, OWNER, KEY, payload(path="CONDITIONALPAY", condition=CONDITION)
    )
    reordered = {"description": "Parcel delivered", "kind": "manual"}
    again, replay = await register_intent(
        session, redis, OWNER, KEY, payload(path="CONDITIONALPAY", condition=reordered)
    )
    assert replay is True and again["id"] == first["id"]


async def test_a_different_description_alone_is_not_a_conflict(repo, redis, session):
    first, _ = await register_intent(session, redis, OWNER, KEY, payload(description="A"))
    again, replay = await register_intent(session, redis, OWNER, KEY, payload(description="B"))
    assert replay is True and again["id"] == first["id"]


# --- merchant order ids: separate purchases vs the same purchase -------------------------


async def test_different_order_ids_with_the_same_amount_are_separate_purchases(
    repo, redis, session
):
    first, first_replay = await register_intent(
        session, redis, OWNER, "key-order-0001", payload(merchant_order_id="order-A")
    )
    second, second_replay = await register_intent(
        session, redis, OWNER, "key-order-0002", payload(merchant_order_id="order-B")
    )
    assert (first_replay, second_replay) == (False, False)
    assert first["id"] != second["id"]
    assert repo.create_calls == 2
    assert repo.events("DUPLICATE_SUPPRESSED") == []


async def test_concurrent_purchases_with_different_order_ids_are_all_created(repo, redis):
    results = await asyncio.gather(
        *[
            register_intent(
                FakeSession(), redis, OWNER, f"key-legit-{i:04d}",
                payload(merchant_order_id=f"order-{i}"),
            )
            for i in range(10)
        ]
    )
    assert all(replay is False for _, replay in results)
    assert len(repo.intents) == 10


async def test_an_order_id_skips_the_similar_payment_guard(repo, redis, session):
    repo.add_row(key="earlier-key-01", age_seconds=5)  # open, same buyer/amount/currency
    intent, replay = await register_intent(
        session, redis, OWNER, KEY, payload(merchant_order_id="order-A")
    )
    assert replay is False
    assert repo.create_calls == 1
    assert repo.events("DUPLICATE_SUPPRESSED") == []


async def test_same_order_id_with_a_new_key_returns_the_existing_payment(repo, redis, session):
    first, _ = await register_intent(
        session, redis, OWNER, "key-order-0001", payload(merchant_order_id="order-A")
    )
    again, replay = await register_intent(
        session, redis, OWNER, "key-order-0002", payload(merchant_order_id="order-A")
    )
    assert replay is True
    assert again["id"] == first["id"]
    assert len(repo.intents) == 1
    assert repo.events("DUPLICATE_SUPPRESSED")[0]["evidence"]["reason"] == "same_order"


@pytest.mark.parametrize(
    "changes",
    [{"amount": "501.00"}, {"currency": "EUR"}, {"path": "CONDITIONALPAY"},
     {"condition": CONDITION}],
    ids=["amount", "currency", "path", "condition"],
)
async def test_same_order_id_with_conflicting_details_raises_payload_mismatch(
    repo, redis, session, changes
):
    await register_intent(
        session, redis, OWNER, "key-order-0001", payload(merchant_order_id="order-A")
    )
    with pytest.raises(PayloadMismatch):
        await register_intent(
            session, redis, OWNER, "key-order-0002",
            payload(merchant_order_id="order-A", **changes),
        )
    assert len(repo.intents) == 1


async def test_order_ids_are_per_owner(repo, redis, session):
    first, _ = await register_intent(
        session, redis, "buyer-1", KEY, payload(merchant_order_id="order-A")
    )
    second, replay = await register_intent(
        session, redis, "buyer-2", KEY, payload(merchant_order_id="order-A")
    )
    assert replay is False and first["id"] != second["id"]


async def test_unique_violation_on_the_order_id_is_treated_as_a_replay(repo, redis, session):
    rival = repo.add_row(key="rival-key-0001", order_id="order-A")
    del repo.intents[(OWNER, "rival-key-0001")]
    repo.competitor = rival  # another process wins the race for the same order id
    intent, replay = await register_intent(
        session, redis, OWNER, KEY, payload(merchant_order_id="order-A")
    )
    assert replay is True
    assert intent["id"] == rival["id"]
    assert repo.events("INTENT_CREATED") == []
    assert repo.events("DUPLICATE_SUPPRESSED")[0]["evidence"]["reason"] == "same_order"


async def test_order_id_race_with_different_details_raises_payload_mismatch(
    repo, redis, session
):
    rival = repo.add_row(key="rival-key-0001", order_id="order-A", amount="999.00")
    del repo.intents[(OWNER, "rival-key-0001")]
    repo.competitor = rival
    with pytest.raises(PayloadMismatch):
        await register_intent(session, redis, OWNER, KEY, payload(merchant_order_id="order-A"))


# --- similar-intent guard (callers that give no order id) --------------------------------


async def test_similar_open_intent_returns_the_open_one(repo, session):
    open_row = repo.add_row(key="older-key-001", age_seconds=30)
    found = await similar_open_intent(session, OWNER, Decimal("500.00"), "USD")
    assert found["id"] == open_row["id"]


async def test_similar_open_intent_returns_the_newest_open_one(repo, session):
    repo.add_row(key="older-key-001", age_seconds=90)
    newest = repo.add_row(key="newer-key-001", age_seconds=10)
    found = await similar_open_intent(session, OWNER, "500.00", "USD")
    assert found["id"] == newest["id"]


@pytest.mark.parametrize("status", sorted(FINAL_STATUSES))
async def test_similar_open_intent_ignores_final_intents(repo, session, status):
    repo.add_row(key="done-key-0001", status=status, age_seconds=10)
    assert await similar_open_intent(session, OWNER, "500.00", "USD") is None


async def test_similar_open_intent_ignores_intents_older_than_120_seconds(repo, session):
    repo.add_row(key="old-key-00001", age_seconds=121)
    assert await similar_open_intent(session, OWNER, "500.00", "USD") is None
    assert await similar_open_intent(session, OWNER, "500.00", "USD", within_seconds=300)


async def test_similar_open_intent_ignores_other_owner_amount_and_currency(repo, session):
    repo.add_row(owner="someone-else", key="k-other-0001")
    repo.add_row(key="k-amount-0001", amount="501.00")
    repo.add_row(key="k-currency-001", currency="EUR")
    assert await similar_open_intent(session, OWNER, "500.00", "USD") is None


async def test_register_with_a_new_key_returns_the_similar_open_intent(repo, redis, session):
    open_row = repo.add_row(key="first-key-001", age_seconds=20)
    intent, replay = await register_intent(session, redis, OWNER, "second-key-01", payload())
    assert replay is True
    assert intent["id"] == open_row["id"]
    assert repo.create_calls == 0
    suppressed = repo.events("DUPLICATE_SUPPRESSED")
    assert len(suppressed) == 1
    assert suppressed[0]["evidence"]["reason"] == "similar_intent"


async def test_register_creates_a_new_intent_when_the_similar_one_is_final(
    repo, redis, session
):
    repo.add_row(key="first-key-001", status="CAPTURED", age_seconds=20)
    intent, replay = await register_intent(session, redis, OWNER, "second-key-01", payload())
    assert replay is False
    assert repo.create_calls == 1
    assert len(repo.intents) == 2


async def test_two_new_keys_with_the_same_amount_at_once_make_one_intent(repo, redis):
    results = await asyncio.gather(
        register_intent(FakeSession(), redis, OWNER, "tap-key-0001", payload()),
        register_intent(FakeSession(), redis, OWNER, "tap-key-0002", payload()),
        return_exceptions=True,
    )
    assert len(repo.intents) == 1
    assert sum(1 for r in results if not isinstance(r, Exception) and r[1] is False) == 1


# --- validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount",
    [0, "0", "0.00", -1, "-5", "abc", "", "1.234", "NaN", "Infinity", "1e2", "1_0", " 10",
     True, None, [], 10**17, "99999999999999999", 0.001, float("nan")],
)
async def test_invalid_amounts_raise_value_error(repo, redis, session, amount):
    with pytest.raises(ValueError):
        await register_intent(session, redis, OWNER, KEY, payload(amount=amount))
    assert repo.intents == {} and repo.audit == []
    assert await redis.keys("*") == []


@pytest.mark.parametrize("amount", ["0.01", "500", "500.5", 500, 500.5, Decimal("12.34")])
async def test_valid_amounts_are_accepted(repo, redis, session, amount):
    intent, _ = await register_intent(session, redis, OWNER, KEY, payload(amount=amount))
    assert intent["amount"] == Decimal(str(amount)).quantize(Decimal("0.01"))


@pytest.mark.parametrize("currency", ["XYZ", "US", "", "USDD", None, 123, "123"])
async def test_invalid_currencies_raise_value_error(repo, redis, session, currency):
    with pytest.raises(ValueError):
        await register_intent(session, redis, OWNER, KEY, payload(currency=currency))
    assert repo.intents == {}


async def test_lowercase_currency_is_normalised(repo, redis, session):
    intent, _ = await register_intent(session, redis, OWNER, KEY, payload(currency="usd"))
    assert intent["currency"] == "USD"


@pytest.mark.parametrize("path", ["FOO", "", None, 5, "SMART PAY"])
async def test_invalid_paths_raise_value_error(repo, redis, session, path):
    with pytest.raises(ValueError):
        await register_intent(session, redis, OWNER, KEY, payload(path=path))


@pytest.mark.parametrize("key", ["", "short", "1234567", "k" * 129, None, 12345678])
async def test_invalid_idempotency_keys_raise_value_error(repo, redis, session, key):
    with pytest.raises(ValueError):
        await register_intent(session, redis, OWNER, key, payload())


@pytest.mark.parametrize("key", ["12345678", "k" * 128])
async def test_idempotency_key_length_bounds_are_inclusive(repo, redis, session, key):
    intent, replay = await register_intent(session, redis, OWNER, key, payload())
    assert replay is False


@pytest.mark.parametrize(
    "bad",
    [
        {"owner_id": ""},
        {"owner_id": "o" * 129},
        {"payload": None},
        {"description": "d" * 201},
        {"description": 5},
        {"condition": "not-a-dict"},
        {"condition": {"when": {1, 2}}},
        {"condition": {"limit": float("nan")}},
        {"condition": {"at": datetime(2026, 1, 1)}},
        {"merchant_order_id": ""},
        {"merchant_order_id": "o" * 129},
        {"merchant_order_id": 42},
    ],
    ids=["owner-empty", "owner-long", "payload-none", "desc-long", "desc-int", "cond-str",
         "cond-set", "cond-nan", "cond-datetime", "order-empty", "order-long", "order-int"],
)
async def test_other_invalid_input_raises_value_error(repo, redis, session, bad):
    owner = bad.get("owner_id", OWNER)
    body = bad["payload"] if "payload" in bad else payload(
        **{k: v for k, v in bad.items() if k != "owner_id"}
    )
    with pytest.raises(ValueError):
        await register_intent(session, redis, owner, KEY, body)
    assert repo.intents == {}


async def test_description_and_condition_are_kept_in_the_audit_event(repo, redis, session):
    await register_intent(session, redis, OWNER, KEY, payload(condition=CONDITION))
    evidence = repo.events("INTENT_CREATED")[0]["evidence"]
    assert evidence["description"] == "Rs 500"
    assert evidence["condition"] == CONDITION
    assert evidence["amount"] == "500.00"  # a string, so the JSON column accepts it


# --- the lock ----------------------------------------------------------------------------


async def test_lock_is_taken_with_an_expiry_and_released_afterwards(redis):
    name = lock_name(OWNER, KEY)
    async with intent_lock(redis, OWNER, KEY, ttl_seconds=30) as token:
        assert len(token) == 32
        assert 0 < await redis.ttl(name) <= 30
    assert await redis.exists(name) == 0


async def test_second_holder_gets_intent_busy_until_release(redis):
    async with intent_lock(redis, OWNER, KEY):
        with pytest.raises(IntentBusy):
            async with intent_lock(redis, OWNER, KEY):
                pass
    async with intent_lock(redis, OWNER, KEY):  # free again
        pass


async def test_locks_for_other_keys_and_owners_do_not_block(redis):
    async with intent_lock(redis, OWNER, KEY):
        async with intent_lock(redis, OWNER, "another-key-01"):
            async with intent_lock(redis, "buyer-2", KEY):
                pass


async def test_lock_cannot_be_released_by_another_holder(redis):
    name = lock_name(OWNER, KEY)
    async with intent_lock(redis, OWNER, KEY):
        await redis.set(name, "someone-elses-token")  # lock expired, another holder took it
    value = await redis.get(name)
    value = value.decode() if isinstance(value, bytes) else value
    assert value == "someone-elses-token"


async def test_expired_lock_retaken_by_another_survives_the_first_holders_release(redis):
    name = lock_name(OWNER, KEY)
    async with intent_lock(redis, OWNER, KEY):
        await redis.delete(name)  # simulate the TTL running out
        async with intent_lock(redis, OWNER, KEY) as second_token:
            pass  # the second holder finishes and releases first
        await redis.set(name, second_token)  # a third holder now owns the key
    value = await redis.get(name)
    value = value.decode() if isinstance(value, bytes) else value
    assert value == second_token


async def test_lock_is_released_even_when_the_body_raises(redis):
    with pytest.raises(RuntimeError):
        async with intent_lock(redis, OWNER, KEY):
            raise RuntimeError("boom")
    assert await redis.exists(lock_name(OWNER, KEY)) == 0


@pytest.mark.parametrize("order_id", [None, "order-A"], ids=["no-order-id", "order-id"])
async def test_register_intent_leaves_no_locks_behind(repo, redis, session, order_id):
    extra = {} if order_id is None else {"merchant_order_id": order_id}
    await register_intent(session, redis, OWNER, KEY, payload(**extra))
    with pytest.raises(PayloadMismatch):
        await register_intent(session, redis, OWNER, KEY, payload(amount="1.00", **extra))
    assert await redis.keys("*") == []


# --- guards against drifting from the rest of the project --------------------------------


def test_paths_match_the_database_constraint():
    constraint = next(
        c for c in Payment.__table__.constraints if c.name == "ck_payments_mode"
    )
    for path in PATHS:
        assert f"'{path}'" in str(constraint.sqltext)


def test_final_statuses_match_the_state_machine_when_it_exists():
    states = pytest.importorskip("backend.state_machine.states")
    final = getattr(states, "FINAL_STATUSES", None)
    if final is None:
        pytest.skip("state machine does not export FINAL_STATUSES")
    assert {str(s) for s in final} == set(FINAL_STATUSES), (
        "idempotency.FINAL_STATUSES differs from backend.state_machine.states.FINAL_STATUSES; "
        "import it instead of keeping a copy"
    )