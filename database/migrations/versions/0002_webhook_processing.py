"""Track durable, exactly-once webhook inbox processing decisions.

Revision ID: 0002_webhook_processing
Revises: 0001_payment_ledger
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_webhook_processing"
down_revision = "0001_payment_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_processing",
        sa.Column(
            "provider_event_id",
            UUID(as_uuid=True),
            sa.ForeignKey("provider_events.id"),
            primary_key=True,
        ),
        sa.Column("payment_id", UUID(as_uuid=True), sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(160), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("webhook_processing")
