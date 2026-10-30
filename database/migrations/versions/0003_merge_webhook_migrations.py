"""Merge the two compatible Task 7 migration histories.

Revision ID: 0003_merge_webhook_migrations
Revises: 0002_provider_event_verify_ms, 0002_webhook_processing
"""

revision = "0003_merge_webhook_migrations"
down_revision = ("0002_provider_event_verify_ms", "0002_webhook_processing")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
