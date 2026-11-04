"""Local observations, distinct from Meta attribution and confirmed sales."""
from datetime import UTC,datetime,timedelta
from loguru import logger
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from app.db.models import ActivityEvent
from app.db.session import session_scope

async def record(source,event_name,event_id,status,**values):
    try:
        async with session_scope() as session:
            await session.execute(insert(ActivityEvent).values(source=source,event_name=event_name,event_id=event_id,
                status=status,**values).on_conflict_do_update(constraint='uq_activity_event',set_={'status':status,'error_code':values.get('error_code')}))
    except Exception:
        logger.warning('Could not persist activity diagnostic')

async def prune_activity(ctx=None):
    async with session_scope() as session:
        await session.execute(delete(ActivityEvent).where(ActivityEvent.created_at<datetime.now(UTC)-timedelta(days=90)))
