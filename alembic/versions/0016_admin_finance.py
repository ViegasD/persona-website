"""Finance metadata and cost provenance; preserve customer and payment records."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0016_admin_finance"
down_revision = "0015_generation_settings"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("api_cost_log", sa.Column("cost_source", sa.String(24), nullable=False, server_default="estimated"), schema="web")
    op.add_column("api_cost_log", sa.Column("provider_request_id", sa.String(128)), schema="web")
    op.add_column("api_cost_log", sa.Column("model", sa.String(128)), schema="web")
    op.add_column("api_cost_log", sa.Column("usage", postgresql.JSONB()), schema="web")
    op.create_index("ix_api_cost_request", "api_cost_log", ["provider", "provider_request_id"], schema="web")
    op.create_unique_constraint("uq_cost_provider_request","api_cost_log",["provider","provider_request_id","operation"],schema="web")
    op.create_table("finance_settings",
        sa.Column("id",sa.Integer(),primary_key=True), sa.Column("revision",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("usd_brl",sa.Numeric(12,6)), sa.Column("estimated_fee_bps",sa.Integer()),
        sa.Column("estimated_fee_fixed_cents",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("overhead_per_video_cents",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("model_rates",postgresql.JSONB(),nullable=False,server_default="{}"),
        sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("id = 1",name="single_finance_settings"),schema="web")
    op.create_table("order_finance_note",
        sa.Column("order_id",sa.BigInteger(),sa.ForeignKey("web.order.id"),primary_key=True),
        sa.Column("is_test",sa.Boolean(),nullable=False,server_default="false"),sa.Column("note",sa.Text()),
        sa.Column("fee_override_cents",sa.Integer()),sa.Column("additional_cost_cents",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),schema="web")
    op.create_table("payment_finance_snapshot",
        sa.Column("payment_id",sa.BigInteger(),sa.ForeignKey("web.payment.id"),primary_key=True),
        sa.Column("fee_cents",sa.Integer()),sa.Column("refunded_cents",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("net_received_cents",sa.Integer()),
        sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),schema="web")

def downgrade():
    for table in ["payment_finance_snapshot","order_finance_note","finance_settings"]: op.drop_table(table,schema="web")
    op.drop_index("ix_api_cost_request",table_name="api_cost_log",schema="web")
    op.drop_constraint("uq_cost_provider_request","api_cost_log",schema="web",type_="unique")
    for name in ["usage","model","provider_request_id","cost_source"]: op.drop_column("api_cost_log",name,schema="web")
