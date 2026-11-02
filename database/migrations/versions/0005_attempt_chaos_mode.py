from alembic import op

revision = "0005_attempt_chaos_mode"
down_revision = "0004_payment_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: migration 0001 builds tables from the current models, so a fresh
    # database may already have the column.
    op.execute("ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS chaos_mode VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE payment_attempts DROP COLUMN IF EXISTS chaos_mode")