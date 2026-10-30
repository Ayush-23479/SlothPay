"""Persist bounded PayPal reconciliation schedules.

Revision ID: 0004_payment_reconciliation
Revises: 0003_merge_webhook_migrations
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0004_payment_reconciliation"
down_revision = "0003_merge_webhook_migrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_reconciliations",
        sa.Column(
            "attempt_id",
            UUID(as_uuid=True),
            sa.ForeignKey("payment_attempts.id"),
            primary_key=True,
        ),
        sa.Column(
            "payment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("payments.id"),
            nullable=False,
        ),
        sa.Column("check_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reason", sa.String(length=160), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("check_count BETWEEN 0 AND 5", name="ck_reconciliation_check_count"),
    )
    op.create_index(
        "ix_payment_reconciliations_payment_id",
        "payment_reconciliations",
        ["payment_id"],
    )
    op.create_index(
        "ix_reconciliation_due",
        "payment_reconciliations",
        ["completed_at", "next_check_at"],
    )
    op.execute(
        """
        INSERT INTO payment_reconciliations
          (attempt_id, payment_id, check_count, next_check_at)
        SELECT id, payment_id, 0, now()
        FROM payment_attempts
        WHERE status IN ('STARTED', 'UNCERTAIN', 'PENDING')
        ON CONFLICT (attempt_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_due", table_name="payment_reconciliations")
    op.drop_index(
        "ix_payment_reconciliations_payment_id", table_name="payment_reconciliations"
    )
    op.drop_table("payment_reconciliations")
