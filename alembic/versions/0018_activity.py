"""Bounded local activity and Meta delivery diagnostics; no historical backfill."""
from alembic import op
import sqlalchemy as sa
revision='0018_activity'
down_revision='0017_operations'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('activity_event',sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('source',sa.String(16),nullable=False),sa.Column('event_id',sa.String(128),nullable=False),
        sa.Column('event_name',sa.String(40),nullable=False),sa.Column('session_id',sa.String(36)),
        sa.Column('user_id',sa.BigInteger(),sa.ForeignKey('web.user.id')),
        sa.Column('order_id',sa.BigInteger(),sa.ForeignKey('web.order.id')),
        sa.Column('page',sa.String(64)),sa.Column('status',sa.String(24),nullable=False),sa.Column('error_code',sa.String(64)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('source','event_id','event_name',name='uq_activity_event'),schema='web')
    op.create_index('ix_activity_event_created_at','activity_event',['created_at'],schema='web')

def downgrade():
    op.drop_table('activity_event',schema='web')
