"""Generate private replacement drafts; only explicit publication changes customer media."""
from datetime import UTC,datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import subprocess
import re
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.db.models import VideoRevision,OrderItem,Order,OrderStatus,OrderItemStatus,Payment,PaymentStatus,OutboxJob,Plan
from app.db.session import session_scope
from app.core.settings import get_settings
from app.services.processing_policy import processing_allowed
from app.services import storage,scene_builder,kie_client,xai_client,scene_images
from app.services.generation_config import read_generation_config
from app.services.cost_tracking import item_context,reconcile_xai_usage
from app.services.outbox import record_delivery_job
from app.services.provider_monitor import report_error
from app.services.video_quality import review_video,VideoQualityError

ACTIVE={'QUEUED','GENERATING','READY','CHECK_REQUIRED'}

async def lock_item(session,item_id):
    oid=(await session.execute(select(OrderItem.order_id).where(OrderItem.id==item_id))).scalar_one_or_none()
    if oid is None:raise HTTPException(404,'Vídeo não encontrado.')
    order=(await session.execute(select(Order).where(Order.id==oid).with_for_update())).scalar_one()
    if not processing_allowed(order):raise HTTPException(409,'Pedido histórico ou processamento pausado. A proteção permanece ativa.')
    if order.status not in {OrderStatus.PAID,OrderStatus.QUEUED,OrderStatus.GENERATING,OrderStatus.READY,OrderStatus.DELIVERED,OrderStatus.FAILED}:
        raise HTTPException(409,'Este pedido não permite geração.')
    approved=(await session.execute(select(Payment.id).where(Payment.order_id==oid,Payment.status==PaymentStatus.APPROVED,Payment.funds_order.is_(True)).limit(1))).scalar_one_or_none()
    if approved is None:raise HTTPException(409,'Pagamento confirmado não encontrado.')
    item=(await session.execute(select(OrderItem).where(OrderItem.id==item_id).with_for_update())).scalar_one()
    return order,item

async def queue_revision(session,item_id,request_key,voice='catalog',reuse_scene=True):
    order,item=await lock_item(session,item_id)
    previous=(await session.execute(select(VideoRevision).where(VideoRevision.request_key==request_key))).scalar_one_or_none()
    if previous:
        if previous.item_id!=item_id:raise HTTPException(409,'Identificador já utilizado.')
        return previous
    if item.status not in {OrderItemStatus.FAILED,OrderItemStatus.READY}:
        raise HTTPException(409,'A geração atual ainda está em andamento.')
    active=(await session.execute(select(VideoRevision.id).where(VideoRevision.item_id==item_id,VideoRevision.status.in_(ACTIVE)))).scalar_one_or_none()
    if active:raise HTTPException(409,'Já existe uma versão pendente. Revise ou descarte antes de gerar outra.')
    if voice!='catalog' and len(item.character_ids)!=1:raise HTTPException(422,'Use as vozes do catálogo para vídeos com vários personagens.')
    revision=VideoRevision(item_id=item_id,request_key=request_key,voice=voice,reuse_scene=reuse_scene,old_video_key=item.video_s3_key,status='QUEUED',image_provider=get_settings().scene_image_provider)
    session.add(revision);await session.flush()
    session.add(OutboxJob(dedup_key=f'regenerate_video:{revision.id}',kind='regenerate_video',order_id=order.id,payload={'revision_id':revision.id}))
    await session.commit();return revision

async def update(revision_id,**values):
    async with session_scope() as session:
        row=await session.get(VideoRevision,revision_id)
        for key,value in values.items():setattr(row,key,value)
        row.updated_at=datetime.now(UTC)

async def generate_video_revision(ctx,revision_id):
    from app.workers.batch_runner import _load_characters,_extract_child_meta,_log_api_cost,_generate_and_store_video
    async with session_scope() as session:
        revision=await session.get(VideoRevision,revision_id)
        if not revision or revision.status not in {'QUEUED','CHECK_REQUIRED'}:return
        try:order,item=await lock_item(session,revision.item_id)
        except HTTPException as error:
            revision.status='FAILED';revision.error=str(error.detail);return
        revision=(await session.execute(select(VideoRevision).where(VideoRevision.id==revision_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        if revision.status not in {'QUEUED','CHECK_REQUIRED'}:return
        if revision.phase in {'scene_submitting','video_submitting'} and not (revision.video_request_id if revision.phase=='video_submitting' else revision.image_request_id):
            revision.status='CHECK_REQUIRED';revision.error='Resultado da submissão desconhecido. Confira o provedor antes de gerar novamente.';return
        config=await read_generation_config(session)
        revision.duration=revision.duration or config.duration;revision.model=revision.model or config.model
        revision.status='GENERATING';revision.error=None
        name,age,occasion,message=_extract_child_meta(item.custom_message)
        name=name or order.recipient_name;age=age or order.recipient_age;occasion=occasion or order.occasion_slug
        item_id=item.id;chars_ids=list(item.character_ids);quality=order.quality
        existing_scene=item.composite_image_s3_key;voice=revision.voice;reuse=revision.reuse_scene
        scene_key=revision.scene_key;image_request_id=revision.image_request_id;video_request_id=revision.video_request_id;video_key=revision.video_key
        image_provider=revision.image_provider or 'kie'
        await session.commit()
    token=item_context.set(item_id)
    try:
        chars=await _load_characters(chars_ids)
        context=scene_builder.scene_context(occasion,message)
        if not scene_key:
            if reuse and existing_scene:
                scene_key=existing_scene
            else:
                if image_provider=='xai':
                    await update(revision_id,phase='scene_submitting')
                    data,image_request_id=await scene_images.generate(scene_builder.scene_prompt([c['name'] for c in chars],occasion,context=context,recipient_name=name),
                        [storage.get_image_as_data_url(c['s3_key']) for c in chars],item_id)
                elif not image_request_id:
                    await update(revision_id,phase='scene_submitting')
                    image_request_id=await kie_client.create_composite_task(prompt=scene_builder.scene_prompt([c['name'] for c in chars],occasion,context=context,recipient_name=name),image_urls=[storage.presigned_get_url(c['s3_key']) for c in chars],submission_attempts=1)
                    await update(revision_id,image_request_id=image_request_id,phase='scene_polling')
                    await _log_api_cost('kie','composite_image',item_id,request_id=image_request_id,model=get_settings().kie_nano_banana_model)
                if image_provider!='xai':
                    result=await kie_client.wait_for_task(image_request_id)
                    data=await xai_client.download_image(kie_client.extract_first_image_url(result))
                scene_builder.validate_scene_dimensions(data)
                scene_key=storage.storefront_key('revisions',str(revision_id),'scene-'+uuid4().hex+'.png')
                storage.upload_bytes(scene_key,data,'image/png')
            await update(revision_id,scene_key=scene_key,phase='scene_review')
        if not video_request_id and not video_key:
            data=storage.download_bytes(scene_key);scene_builder.validate_scene_dimensions(data)
            # Reused older scenes may predate name personalization; review new scenes against the new lettering requirement.
            await scene_builder.review_scene([storage.get_image_as_data_url(c['s3_key']) for c in chars],storage.get_image_as_data_url(scene_key),context=context,recipient_name=name if not (reuse and existing_scene==scene_key) else None)
            await update(revision_id,phase='script')
            await _generate_and_store_video(item_id=item_id,chars=chars,input_image_url=storage.get_image_as_data_url(scene_key),recipient_name=name,recipient_age=age,occasion_slug=occasion,custom_message=message,quality=quality,revision_id=revision_id,voice_override=voice)
        elif video_request_id and not video_key:
            result=await xai_client.wait_for_video(video_request_id);await reconcile_xai_usage(video_request_id,result)
            async with session_scope() as session:revision=await session.get(VideoRevision,revision_id);duration=revision.duration
            data=await xai_client.download_video(result['video']['url'],expected_duration=duration)
            video_key=storage.storefront_key('revisions',str(revision_id),uuid4().hex+'.mp4');storage.upload_bytes(video_key,data,'video/mp4')
            await update(revision_id,video_key=video_key,phase='video_stored')
        async with session_scope() as session:revision=await session.get(VideoRevision,revision_id);video_key=revision.video_key
        await update(revision_id,phase='video_review')
        raw=storage.download_bytes(video_key)
        try:verdict=await review_video(raw,storage.get_image_as_data_url(scene_key))
        except VideoQualityError as error:
            import json
            storage.upload_bytes(video_key+'.quality.json',json.dumps(error.verdict).encode(),'application/json');raise
        import json
        storage.upload_bytes(video_key+'.quality.json',json.dumps(verdict).encode(),'application/json')
        with TemporaryDirectory() as directory:
            path=Path(directory)/'video.mp4';path.write_bytes(raw);thumbnail=Path(directory)/'frame.jpg'
            subprocess.run(['ffmpeg','-v','error','-ss','1','-i',str(path),'-frames:v','1','-y',str(thumbnail)],capture_output=True,check=True,timeout=120)
            key=storage.storefront_key('revisions',str(revision_id),'thumbnail-'+uuid4().hex+'.jpg');storage.upload_bytes(key,thumbnail.read_bytes(),'image/jpeg')
        await update(revision_id,thumbnail_key=key,status='READY',phase='review',error=None)
    except Exception as error:
        await report_error(error)
        async with session_scope() as session:revision=await session.get(VideoRevision,revision_id)
        ambiguous=(bool(revision.video_request_id or revision.video_key) or
                   (revision.phase in {'scene_submitting','video_submitting'} and not isinstance(error,xai_client.XaiError)))
        if revision.phase=='video_submitting' and isinstance(error,xai_client.XaiError):
            # Only explicit non-5xx rejection proves no task was accepted.
            ambiguous=not bool(re.search(r'xAI HTTP [234]\d\d',str(error)))
        if revision.phase=='scene_submitting' and isinstance(error,xai_client.XaiError):ambiguous=not bool(re.search(r'xAI HTTP [234]\d\d',str(error)))
        if revision.phase=='scene_submitting' and isinstance(error,kie_client.KieError) and 'credit_balance_exhausted' in str(error):ambiguous=False
        if isinstance(error,xai_client.XaiError) and ('outcome unknown' in str(error) or 'polling' in str(error) or 'timed out' in str(error)):ambiguous=True
        if isinstance(error,kie_client.KieError) and 'timed out' in str(error):ambiguous=True
        safe=re.sub(r'https?://\S+','[URL omitida]',str(error))[:400]
        await update(revision_id,status='CHECK_REQUIRED' if ambiguous else 'FAILED',error=safe or type(error).__name__)
    finally:item_context.reset(token)

async def publish(session,revision_id):
    revision=await session.get(VideoRevision,revision_id)
    if not revision:raise HTTPException(404,'Versão não encontrada.')
    order,item=await lock_item(session,revision.item_id)
    revision=(await session.execute(select(VideoRevision).where(VideoRevision.id==revision_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    if revision.status=='PUBLISHED':return revision
    if revision.status!='READY' or not revision.video_key:raise HTTPException(409,'A versão ainda não está pronta para publicação.')
    if item.video_s3_key!=revision.old_video_key:raise HTTPException(409,'Outro vídeo já foi publicado. Atualize antes de continuar.')
    item.video_s3_key=revision.video_key;item.thumbnail_s3_key=revision.thumbnail_key;item.composite_image_s3_key=revision.scene_key
    item.comfy_workflow_b_prompt_id=revision.video_request_id;item.resolved_script=revision.prompt;item.status=OrderItemStatus.READY;item.error=None
    revision.status='PUBLISHED';revision.published_at=datetime.now(UTC)
    await session.flush()
    items=(await session.execute(select(OrderItem).where(OrderItem.order_id==order.id))).scalars().all()
    expected=(await session.execute(select(Plan.video_count).where(Plan.id==order.plan_id))).scalar_one()
    complete=len(items)==expected and expected>0 and all(i.status==OrderItemStatus.READY and i.video_s3_key for i in items)
    if complete and order.status!=OrderStatus.DELIVERED:
        order.status=OrderStatus.READY;order.error=None;order.generated_at=datetime.now(UTC);await record_delivery_job(session,order)
    await session.commit();return revision
