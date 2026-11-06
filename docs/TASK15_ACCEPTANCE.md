# Task 15 — Integration and End-to-End Acceptance

## Scope

Validate the integrated SlothPay payment system using a
controlled PayPal simulator, isolated PostgreSQL test data,
and separate real PayPal Sandbox manual acceptance.

## Verified Baseline

- Ruff: passed
- Backend unit tests: 844 passed
- Backend unit tests: 31 skipped
- Backend unit tests: 1 third-party deprecation warning

## Required Integration Scenarios

- [ ] SmartPay completes successfully
- [ ] ConditionalPay captures after verified condition
- [ ] ConditionalPay voids after failed condition
- [ ] ConditionalPay voids after expiry
- [ ] Lost PayPal answer recovers without another capture
- [ ] 50 simultaneous requests create one payment
- [ ] Forged webhook cannot change payment state
- [ ] Repeated webhook has no duplicate effect
- [ ] Out-of-order webhook cannot cause an invalid transition
- [ ] Payment decline is handled safely
- [ ] Trust Passport integrity chain validates
- [ ] Tampered evidence fails integrity verification

## Stress Acceptance

- [ ] 100 simulated payment scenarios executed
- [ ] Controlled random failures exercised
- [ ] Zero duplicate payments verified
- [ ] Zero duplicate capture operations verified
- [ ] Simulator metrics recorded separately from Sandbox

## Real PayPal Sandbox Acceptance

- [ ] Manual approval script reviewed
- [ ] Sandbox-only endpoint verified
- [ ] Manual approval link generated
- [ ] Sandbox payment outcome independently verified

## Safety Requirements

- Never run destructive tests against the development database.
- Never retry ambiguous captures without provider evidence.
- Do not commit PayPal credentials or .env files.
- Label all simulated results as simulator results.
- Do not claim skipped tests passed.

## Final Status

IN PROGRESS — Integration and stress acceptance pending.

## Verified Integration Results — November 7, 2026

- Ruff validation: PASSED.
- Backend unit suite: 844 passed, 31 skipped.
- PostgreSQL + Redis integration suite: 126 passed.
- Temporal ConditionalPay expiry regression: PASSED.
- Integration duration: 191.54 seconds.
- Integration warnings: 1 third-party Starlette deprecation warning.

### Temporal Regression Fix

Updated the ConditionalPay expiry integration test to explicitly
advance Temporal's simulated clock beyond the condition deadline.

The test verifies:
- expired condition triggers void
- payment reaches VOIDED
- condition reaches EXPIRED
- authorization is voided exactly once
- capture is never attempted

### Outstanding Task 15 Acceptance

- 100-payment randomized fake-PayPal simulator: NOT YET VERIFIED.
- Zero-duplicate 100-payment stress report: NOT YET VERIFIED.
- Manual real PayPal Sandbox approval: NOT YET VERIFIED.

The integration suite uses local PostgreSQL, Redis, and test
gateway behavior. It is not evidence of real Sandbox acceptance.

### Current Status

IN PROGRESS — Integration tests verified; stress and Sandbox
acceptance remain outstanding.

## Task 15 Simulator Results — Verified

- Date: November 7, 2026
- Provider: Fake PayPal (SIMULATOR ONLY)
- Test: test_task15_100_payment_simulator
- Payment scenarios: 100
- Successful captures: 79
- Simulated not-sent failures: 21
- Duplicate payment records: 0
- Duplicate outbound order creations: 0
- Duplicate outbound captures: 0
- Test outcome: 1 passed in 21.12 seconds

These results do not establish real PayPal Sandbox acceptance.

### Remaining
- Randomized lost-answer recovery stress coverage.
- Manual real Sandbox approval and provider verification.

## Randomized Lost-Answer Stress Acceptance

- Test: test_task15_randomized_lost_answer_stress
- Scenarios: 100 fake-PayPal payments
- Seed: 1515
- Simulated failure: provider completes capture but response is lost
- Recovery: provider-state reconciliation
- Duplicate payment records: 0
- Duplicate outbound captures: 0
- Automated test: PASSED
- Real Sandbox acceptance: PENDING manual verification

This is simulator evidence, not real PayPal Sandbox evidence.
