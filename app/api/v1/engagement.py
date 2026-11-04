from datetime import UTC,datetime
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select,and_,or_,func
from sqlalchemy.dialects.postgresql import insert
from app.api.deps import require_user
from app.db.session import get_session
from app.db.models import User,Order,OrderItem,OrderStatus,OrderItemStatus,VideoEngagement
from app.db.models import ActivityEvent
from app.services.engagement import video_version

router=APIRouter(prefix='/engagement',tags=['customer-engagement'])
class Event(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:str=Field(pattern=r'^[a-f0-9]{64}$')
    event:Literal['viewed','completed','downloaded']
class Rating(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:str=Field(pattern=r'^[a-f0-9]{64}$')
    score:int=Field(ge=1,le=5,strict=True)
    comment:str|None=Field(None,max_length=1000)

async def owned_item(session,item_id,user_id,version):
    from app.api.v1.orders import _load_order
    item=await session.get(OrderItem,item_id)
    if not item:raise HTTPException(404,'Vídeo não encontrado.')
    order=await _load_order(session,item.order_id,user_id=user_id,guest_order_ids=[])
    if order.status not in {OrderStatus.READY,OrderStatus.DELIVERED} or item.status!=OrderItemStatus.READY or not item.video_s3_key:
        raise HTTPException(409,'Vídeo indisponível.')
    if video_version(item)!=version:raise HTTPException(409,'Uma nova versão está disponível. Atualize a página.')
    return item

@router.get('/mine')
async def mine(user_id:int=Depends(require_user),session=Depends(get_session)):
    user=await session.get(User,user_id)
    ownership=Order.user_id==user_id
    if user and user.email_verified_at and user.email:
        ownership=or_(ownership,and_(Order.user_id.is_(None),func.lower(func.trim(Order.guest_email))==user.email.strip().lower()))
    items=(await session.execute(select(OrderItem).join(Order).where(ownership,Order.status.in_([OrderStatus.READY,OrderStatus.DELIVERED])).limit(2000))).scalars().all()
    records=(await session.execute(select(VideoEngagement).where(VideoEngagement.user_id==user_id,VideoEngagement.item_id.in_([i.id for i in items])))).scalars().all()
    indexed={(r.item_id,r.version):r for r in records}
    return {i.id:{'version':video_version(i),**({k:getattr(indexed[(i.id,video_version(i))],k) for k in ['viewed_at','completed_at','downloaded_at','score','comment','rated_at']} if (i.id,video_version(i)) in indexed else {})} for i in items}

@router.post('/items/{item_id}/events')
async def event(item_id:int,values:Event,user_id:int=Depends(require_user),session=Depends(get_session)):
    await owned_item(session,item_id,user_id,values.version)
    field={'viewed':'viewed_at','completed':'completed_at','downloaded':'downloaded_at'}[values.event]
    now=datetime.now(UTC)
    changes={field:func.coalesce(getattr(VideoEngagement,field),now)}
    data={'item_id':item_id,'user_id':user_id,'version':values.version,field:now}
    await session.execute(insert(VideoEngagement).values(**data).on_conflict_do_update(index_elements=['item_id','user_id','version'],set_=changes))
    await session.execute(insert(ActivityEvent).values(source='server',event_name={'viewed':'VideoViewed','completed':'VideoCompleted','downloaded':'VideoDownloadClicked'}[values.event],
        event_id=f'item_{item_id}_{user_id}_{values.version}',user_id=user_id,status='confirmed').on_conflict_do_nothing(constraint='uq_activity_event'))
    await session.commit();return {'recorded':True}

@router.put('/items/{item_id}/rating')
async def rate(item_id:int,values:Rating,user_id:int=Depends(require_user),session=Depends(get_session)):
    await owned_item(session,item_id,user_id,values.version)
    now=datetime.now(UTC);data={'score':values.score,'comment':(values.comment or '').strip() or None,'rated_at':now}
    await session.execute(insert(VideoEngagement).values(item_id=item_id,user_id=user_id,version=values.version,**data)
        .on_conflict_do_update(index_elements=['item_id','user_id','version'],set_=data))
    await session.execute(insert(ActivityEvent).values(source='server',event_name='VideoRated',event_id=f'item_{item_id}_{user_id}_{values.version}',
        user_id=user_id,status='confirmed').on_conflict_do_nothing(constraint='uq_activity_event'))
    await session.commit();return {'saved':True,**data}
