from alembic import op

revision = "0002_provider_event_verify_ms"
down_revision = "0001_payment_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE provider_events ADD COLUMN IF NOT EXISTS verify_ms INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE provider_events DROP COLUMN IF EXISTS verify_ms")