"""Authenticated operational controls; paid work is staged until explicit approval."""
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select

from app.api.deps import require_admin_key
from app.core.security import sign_revision_token
from app.core.settings import get_settings
from app.db.models import Order, OrderItem, OutboxJob, VideoEngagement, VideoRevision
from app.db.session import get_session
from app.services import provider_monitor, video_revisions
from app.services.engagement import video_version
from app.services.processing_policy import processing_allowed

router = APIRouter(prefix='/admin', tags=['admin-operations'], dependencies=[Depends(require_admin_key)])


def revision_out(row):
    token = sign_revision_token(row.id)
    url = f'{get_settings().api_base_url.rstrip("/")}/api/v1/media/revisions/{token}'
    return dict(id=row.id, item_id=row.item_id, status=row.status, voice=row.voice,
                reuse_scene=row.reuse_scene, phase=row.phase, error=row.error,
                duration=row.duration, model=row.model, created_at=row.created_at,
                updated_at=row.updated_at, published_at=row.published_at,
                video_url=url if row.video_key else None,
                scene_url=url+'?scene=true' if row.scene_key else None)


@router.get('/operations')
async def operations(session=Depends(get_session)):
    return await provider_monitor.read(session)


@router.put('/operations/settings')
async def settings(values: provider_monitor.MonitorValues, session=Depends(get_session)):
    return await provider_monitor.save_settings(session, values)


@router.post('/operations/refresh-balances')
async def refresh(session=Depends(get_session)):
    await provider_monitor.check_provider_balances()
    return await provider_monitor.read(session)


@router.get('/orders/{order_id}/operations')
async def order_operations(order_id: int, session=Depends(get_session)):
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(404, 'Pedido não encontrado.')
    items = (await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))).scalars().all()
    ids = [i.id for i in items]
    revisions = (await session.execute(select(VideoRevision).where(VideoRevision.item_id.in_(ids))
                                      .order_by(VideoRevision.id.desc()).limit(200))).scalars().all()
    # Engagement is deliberately separate from media access logs and admin previews.
    events = (await session.execute(select(VideoEngagement).where(VideoEngagement.item_id.in_(ids)))).scalars().all()
    result = []
    for item in items:
        current = [e for e in events if e.item_id == item.id and e.version == video_version(item)]
        first = lambda field: min((getattr(e, field) for e in current if getattr(e, field)), default=None)
        result.append(dict(item_id=item.id, viewed_at=first('viewed_at'), completed_at=first('completed_at'),
                           downloaded_at=first('downloaded_at'), ratings=[dict(score=e.score, comment=e.comment,
                           rated_at=e.rated_at) for e in current if e.score is not None]))
    return dict(processing_allowed=processing_allowed(order), items=result,
                revisions=[revision_out(r) for r in revisions])


class RevisionValues(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_key: UUID
    voice: Literal['catalog', 'male', 'female'] = 'catalog'
    reuse_scene: StrictBool = True


@router.post('/items/{item_id}/revisions')
async def create(item_id: int, values: RevisionValues, session=Depends(get_session)):
    row = await video_revisions.queue_revision(session, item_id, str(values.request_key), values.voice, values.reuse_scene)
    return revision_out(row)


@router.post('/revisions/{revision_id}/publish')
async def publish(revision_id: int, session=Depends(get_session)):
    return revision_out(await video_revisions.publish(session, revision_id))


async def locked_revision(session, revision_id):
    row = await session.get(VideoRevision, revision_id)
    if row is None:
        raise HTTPException(404, 'Versão não encontrada.')
    order, item = await video_revisions.lock_item(session, row.item_id)
    row = (await session.execute(select(VideoRevision).where(VideoRevision.id == revision_id)
                 .with_for_update().execution_options(populate_existing=True))).scalar_one()
    return order, row


@router.post('/revisions/{revision_id}/discard')
async def discard(revision_id: int, session=Depends(get_session)):
    _, row = await locked_revision(session, revision_id)
    if row.status not in {'QUEUED', 'READY', 'FAILED', 'CHECK_REQUIRED'}:
        raise HTTPException(409, 'Não é possível descartar uma geração em andamento ou já publicada.')
    row.status = 'DISCARDED'; row.updated_at = datetime.now(UTC)
    await session.commit()
    return revision_out(row)


@router.post('/revisions/{revision_id}/resume')
async def resume(revision_id: int, session=Depends(get_session)):
    order, row = await locked_revision(session, revision_id)
    stale = row.status == 'GENERATING' and row.updated_at < datetime.now(UTC)-timedelta(minutes=40)
    if row.status != 'CHECK_REQUIRED' and not stale:
        raise HTTPException(409, 'A versão não está aguardando recuperação.')
    if row.phase in {'scene_submitting', 'video_submitting'} and not (
            row.video_request_id if row.phase == 'video_submitting' else row.image_request_id):
        raise HTTPException(409, 'Submissão sem confirmação. Confira o histórico no provedor antes de descartar e gerar outra versão; uma nova geração poderá ser cobrada.')
    row.status = 'CHECK_REQUIRED'; row.updated_at = datetime.now(UTC)
    session.add(OutboxJob(dedup_key=f'resume_revision:{row.id}:{uuid4()}', kind='regenerate_video',
                         order_id=order.id, payload={'revision_id':row.id}))
    await session.commit()
    return revision_out(row)
