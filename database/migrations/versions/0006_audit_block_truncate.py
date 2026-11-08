from alembic import op

revision = "0006_audit_block_truncate"
down_revision = "0005_attempt_chaos_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS payment_audit_no_truncate ON payment_audit")
    op.execute("""
    CREATE TRIGGER payment_audit_no_truncate
    BEFORE TRUNCATE ON payment_audit
    FOR EACH STATEMENT EXECUTE FUNCTION slothpay_block_audit_mutation();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS payment_audit_no_truncate ON payment_audit")