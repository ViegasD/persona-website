from datetime import UTC,datetime,timedelta
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Header,Query
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select,func,distinct
from sqlalchemy.dialects.postgresql import insert
from app.api.deps import require_admin_key,get_current_user_id
from app.core.settings import get_settings
from app.db.models import ActivityEvent,User,Order,OrderItem,Payment,PaymentStatus,VideoEngagement
from app.db.session import get_session

router=APIRouter(tags=['activity'])
class BrowserEvent(BaseModel):
    model_config=ConfigDict(extra='forbid')
    event_id:str=Field(min_length=1,max_length=128,pattern=r'^[a-zA-Z0-9_.:-]+$')
    session_id:UUID
    event_name:Literal['PageView','ViewContent','AddToCart','CheckoutProgress','AddPaymentInfo','InitiateCheckout','Purchase']
    page:Literal['/','/index.html','/checkout.html','/conta.html','/meus-videos.html','/aguardando.html','/character.html']
    status:Literal['local_only','pixel_queued','pixel_loaded']

@router.post('/activity/events',status_code=202)
async def browser_event(values:BrowserEvent,origin:str|None=Header(None),user_id:int|None=Depends(get_current_user_id),session=Depends(get_session)):
    allowed={value.strip().rstrip('/') for value in get_settings().web_base_url.split(',')}
    if not origin or origin.rstrip('/') not in allowed:raise HTTPException(403,'invalid origin')
    sid=str(values.session_id)
    count=(await session.execute(select(func.count()).select_from(ActivityEvent).where(ActivityEvent.source=='browser',
        ActivityEvent.session_id==sid,ActivityEvent.created_at>datetime.now(UTC)-timedelta(days=1)))).scalar_one()
    if count>=200:raise HTTPException(429,'session event limit')
    data=values.model_dump(exclude={'session_id'});data.update(session_id=sid,user_id=user_id,source='browser')
    # Browser claims cannot confirm purchases or change any order/payment.
    await session.execute(insert(ActivityEvent).values(**data).on_conflict_do_nothing(constraint='uq_activity_event'))
    await session.commit();return {'recorded':True}

@router.get('/admin/activity',dependencies=[Depends(require_admin_key)])
async def report(days:int=Query(7,ge=1,le=90),session=Depends(get_session)):
    since=datetime.now(UTC)-timedelta(days=days)
    grouped=(await session.execute(select(ActivityEvent.source,ActivityEvent.event_name,ActivityEvent.status,func.count())
        .where(ActivityEvent.created_at>=since).group_by(ActivityEvent.source,ActivityEvent.event_name,ActivityEvent.status))).all()
    recent=(await session.execute(select(ActivityEvent,User.email).outerjoin(User,User.id==ActivityEvent.user_id)
        .where(ActivityEvent.created_at>=since).order_by(ActivityEvent.id.desc()).limit(100))).all()
    sessions=(await session.execute(select(func.count(distinct(ActivityEvent.session_id))).where(
        ActivityEvent.source=='browser',ActivityEvent.created_at>=since))).scalar_one()
    accounts=(await session.execute(select(func.count(),func.count(User.email_verified_at)).where(User.created_at>=since))).one()
    paid=select(Payment.id).where(Payment.order_id==Order.id,Payment.status.in_([PaymentStatus.APPROVED,PaymentStatus.REFUNDED]),Payment.funds_order.is_(True)).exists()
    orders=(await session.execute(select(func.count(),func.count().filter(paid),func.count().filter(Order.delivered_at.is_not(None)))
        .where(Order.created_at>=since))).one()
    engagement=(await session.execute(select(func.count(distinct(VideoEngagement.item_id)).filter(VideoEngagement.viewed_at>=since),
        func.count(distinct(VideoEngagement.item_id)).filter(VideoEngagement.completed_at>=since),
        func.count().filter(VideoEngagement.rated_at>=since),func.avg(VideoEngagement.score).filter(VideoEngagement.rated_at>=since)))).one()
    first=(await session.execute(select(func.min(ActivityEvent.created_at)))).scalar_one()
    pages=(await session.execute(select(ActivityEvent.page,func.count()).where(ActivityEvent.source=='browser',ActivityEvent.event_name=='PageView',ActivityEvent.created_at>=since)
        .group_by(ActivityEvent.page).order_by(func.count().desc()))).all()
    settings=get_settings()
    return dict(days=days,tracking_since=first,pixel_id=settings.facebook_pixel_id,capi_configured=bool(settings.facebook_pixel_id and settings.facebook_capi_token),
        sessions=sessions,accounts_created=accounts[0],accounts_verified=accounts[1],orders_created=orders[0],orders_paid=orders[1],orders_delivered=orders[2],
        videos_viewed=engagement[0],videos_completed=engagement[1],ratings_count=engagement[2],rating_average=engagement[3],
        events=[dict(source=a,event_name=b,status=c,count=d) for a,b,c,d in grouped],pages=[dict(page=a,count=b) for a,b in pages],
        recent=[dict(id=e.id,source=e.source,event_name=e.event_name,status=e.status,page=e.page,user_id=e.user_id,email=email,
            order_id=e.order_id,event_id=e.event_id,error_code=e.error_code,created_at=e.created_at) for e,email in recent])
