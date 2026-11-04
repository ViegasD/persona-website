"""Admin video revisions, provider monitoring and customer feedback; additive only."""
from alembic import op
import sqlalchemy as sa

revision="0017_operations"
down_revision="0016_admin_finance"
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('video_revision',
        sa.Column('id',sa.BigInteger(),primary_key=True),sa.Column('item_id',sa.BigInteger(),sa.ForeignKey('web.order_item.id'),nullable=False),
        sa.Column('request_key',sa.String(36),nullable=False),sa.Column('status',sa.String(24),nullable=False,server_default='QUEUED'),
        sa.Column('voice',sa.String(16),nullable=False,server_default='catalog'),sa.Column('old_video_key',sa.String(512)),
        sa.Column('reuse_scene',sa.Boolean(),nullable=False,server_default='true'),
        *[sa.Column(name,sa.String(512)) for name in ['scene_key','video_key','thumbnail_key']],
        sa.Column('prompt',sa.Text()),sa.Column('image_request_id',sa.String(128)),sa.Column('image_provider',sa.String(16)),sa.Column('video_request_id',sa.String(128)),
        sa.Column('phase',sa.String(32)),sa.Column('duration',sa.Integer()),sa.Column('model',sa.String(128)),sa.Column('error',sa.String(500)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),sa.Column('published_at',sa.DateTime(timezone=True)),
        sa.UniqueConstraint('request_key',name='uq_revision_request'),schema='web')
    op.create_index('ix_video_revision_item_id','video_revision',['item_id'],schema='web')
    op.create_index('uq_active_video_revision','video_revision',['item_id'],unique=True,schema='web',postgresql_where=sa.text("status IN ('QUEUED','GENERATING','READY','CHECK_REQUIRED')"))
    op.create_table('video_engagement',sa.Column('item_id',sa.BigInteger(),sa.ForeignKey('web.order_item.id'),primary_key=True),
        sa.Column('user_id',sa.BigInteger(),sa.ForeignKey('web.user.id'),primary_key=True),sa.Column('version',sa.String(64),primary_key=True),
        *[sa.Column(name,sa.DateTime(timezone=True)) for name in ['viewed_at','completed_at','downloaded_at']],
        sa.Column('score',sa.Integer()),sa.Column('comment',sa.String(1000)),sa.Column('rated_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint('score BETWEEN 1 AND 5',name='ck_video_rating'),schema='web')
    settings=op.create_table('operations_settings',sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('revision',sa.Integer(),nullable=False,server_default='0'),sa.Column('staff_email',sa.String(255),nullable=False,server_default='eduardolv100@gmail.com'),
        sa.Column('alerts_enabled',sa.Boolean(),nullable=False,server_default='true'),sa.CheckConstraint('id=1',name='single_operations_settings'),schema='web')
    providers=op.create_table('provider_account',sa.Column('provider',sa.String(16),primary_key=True),sa.Column('threshold',sa.Numeric(18,6),nullable=False),
        sa.Column('manual_balance',sa.Numeric(18,6)),sa.Column('manual_at',sa.DateTime(timezone=True)),sa.Column('balance',sa.Numeric(18,6)),
        *[sa.Column(name,sa.DateTime(timezone=True)) for name in ['checked_at','last_success_at','last_alert_at']],
        sa.Column('quota_exhausted',sa.Boolean(),nullable=False,server_default='false'),sa.Column('error',sa.String(200)),sa.Column('alert_episode',sa.String(36)),schema='web')
    op.bulk_insert(settings,[{'id':1}])
    op.bulk_insert(providers,[{'provider':'openai','threshold':5},{'provider':'xai','threshold':5},{'provider':'kie','threshold':100}])

def downgrade():
    for table in ['provider_account','operations_settings','video_engagement','video_revision']:op.drop_table(table,schema='web')
