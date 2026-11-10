from alembic import op

revision = "0007_payment_timings"
down_revision = "0006_audit_block_truncate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this safe to repeat. The table is not part of Base.metadata, so
    # migration 0001 does not create it.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_timings (
            id BIGSERIAL PRIMARY KEY,
            intent_id UUID NOT NULL REFERENCES payments (id),
            step VARCHAR(48) NOT NULL,
            kind VARCHAR(16) NOT NULL
                CHECK (kind IN ('ours', 'provider', 'human', 'wait')),
            ms NUMERIC(12, 3) NOT NULL CHECK (ms >= 0),
            ts TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payment_timings_intent ON payment_timings (intent_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payment_timings_step_ts ON payment_timings (step, ts)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS payment_timings")