# State machine contract: who enforces what

The state machine (`backend/state_machine`) is **pure**: it has no database and no network. It
decides what is *allowed*. It cannot make a decision *atomic*, and it cannot see PayPal. Those
two jobs belong to other layers, and this page says exactly where.

## The boundary

| Guarantee | Enforced by | Where |
| --- | --- | --- |
| Only legal status moves | `validate_transition`, `claim_status_for` | `backend/state_machine` |
| Only one worker can claim a money move | `transition_payment` (compare-and-swap on `status` AND `version`) | `backend/models/repository.py` (Task 2) |
| The claim is durable before PayPal is called | Orchestration commits the claim, then calls the gateway | `backend/orchestration` (Task 6) |
| The authorization is valid and not expired | `authorization_capturable` on a fresh PayPal read | read in `backend/payments` (Task 4), called from Task 6 |
| Stable PayPal request id on every money call | `derive_request_id` | `backend/reliability/idempotency.py` (Task 5) |

## Claim-then-execute (required order for capture, void and authorize)

1. **Read** the payment (`status`, `version`). For capture, also read the authorization from PayPal
   (GET) and set `authorization_valid = authorization_capturable(response)`.
2. **Decide** with `new_status = claim_status_for(path, status, operation, context)`.
   It raises `OperationNotPermitted` if the move is not allowed. Capture needs
   `condition_verified is True` AND `authorization_valid is True`.
3. **Claim** with `transition_payment(expected_version, expected_status=status, new_status)`
   and **commit**. If it raises `StalePaymentVersion`, another worker already moved the payment:
   **do not call PayPal**. Re-read and reconcile.
4. **Execute** the PayPal call with the stable request id, only now.
5. **Record** the outcome with another validated, version-checked transition (or `UNCERTAIN`).

Why a race cannot do both capture and void: both claims start from the same
`(AWAITING_CONDITION, version)`. The database lets exactly one `UPDATE ... WHERE status AND
version` succeed. The loser gets `StalePaymentVersion` and never reaches step 4. Also, neither
`CAPTURE_REQUESTED` nor `VOID_REQUESTED` has a transition to the other.

## Rules for later tasks

- No code may call a gateway capture, void or authorize method unless step 3 has committed.
- `allowed_actions` is advice, not a lock. Never act on it without the claim.
- `authorization_valid` must come from a read made in the same operation, not from a cached or
  earlier value. Expiry is checked with a safety margin (`DEFAULT_MIN_REMAINING`, 5 minutes).
- PayPal field names used by `authorization_capturable` (`status`, `expiration_time`) must be
  confirmed against a real sandbox response in Task 4.

## Tests that cover this

| Guarantee | Test |
| --- | --- |
| Capture and void claims are mutually exclusive statuses | `test_capture_and_void_claims_are_mutually_exclusive_statuses` |
| Capture refused without verified condition AND confirmed authorization | `test_capture_is_refused_unless_condition_and_authorization_are_both_confirmed` |
| Expired authorization never offers capture; void stays possible | `test_expired_or_invalid_authorization_never_offers_capture`, `test_void_stays_possible_when_the_authorization_cannot_be_captured` |
| Authorization check fails closed (missing, past, near expiry, wrong status) | `test_authorization_capturable_fails_closed` |
| No claim is ever an illegal transition | `test_claim_status_for_never_returns_a_status_validate_transition_would_refuse` |
| Real database: only one of capture/void wins, the loser gets `StalePaymentVersion`, one audit row | `tests/integration/test_claim_race.py` (needs PostgreSQL, `SLOTHPAY_RUN_DB_TESTS=1`) |
| Stale version rejected | `tests/integration/test_payment_ledger.py::test_stale_version_rejected` (Task 2) |

**Added in Task 6 (not yet written):** the orchestration tests that prove the gateway is never
called when the claim loses, and that capture reads a fresh authorization first.