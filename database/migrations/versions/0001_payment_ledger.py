"""Create SlothPay payment ledger and database-enforced audit immutability.

Revision ID: 0001_payment_ledger
Revises:
"""

from alembic import op

from backend.models import Base

revision = "0001_payment_ledger"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Explicit metadata in a versioned migration; do not call create_all at runtime.
    for table in (t for t in Base.metadata.sorted_tables if t.name != "payment_reconciliations"):
        table.create(bind=op.get_bind(), checkfirst=False)
    op.execute("""
    CREATE FUNCTION slothpay_block_audit_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'payment_audit is append-only';
    END;
    $$;
    """)
    op.execute("""
    CREATE TRIGGER payment_audit_immutable
    BEFORE UPDATE OR DELETE ON payment_audit
    FOR EACH ROW EXECUTE FUNCTION slothpay_block_audit_mutation();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS payment_audit_immutable ON payment_audit")
    op.execute("DROP FUNCTION IF EXISTS slothpay_block_audit_mutation()")
    for table in reversed(
        [t for t in Base.metadata.sorted_tables if t.name != "payment_reconciliations"]
    ):
        table.drop(bind=op.get_bind(), checkfirst=False)
