"""Paid recovery boundaries, truthful budgets, engagement ownership and image contracts."""
import io
import uuid
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock,Mock
import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select,func

from app.core.settings import get_settings
from app.core.security import sign_revision_token,verify_media_token,verify_revision_token
from app.db.models import Order,OrderItem,OrderStatus,OrderItemStatus,User,Payment,PaymentStatus,VideoRevision,VideoEngagement,ProviderAccount,OperationsSettings,OutboxJob,ActivityEvent
from app.services import provider_monitor,video_revisions,scene_images,script_writer,scene_builder
from app.api.v1 import engagement,admin_operations,activity
from tests.test_postgres_lifecycle import database,make_orders


def test_private_revision_tokens_are_not_customer_tokens():
    token=sign_revision_token(42)
    assert verify_revision_token(token)==42 and verify_media_token(token) is None
    assert verify_revision_token(token+'x') is None


@pytest.mark.parametrize('value',[True,'NaN','Infinity',-1])
def test_provider_balance_rejects_invalid_values(value):
    with pytest.raises((ValueError,ArithmeticError)):provider_monitor.decimal_balance(value)


def test_quota_errors_distinguish_rate_limits_from_exhaustion():
    from app.services.xai_client import XaiError
    from app.services.kie_client import KieError
    assert provider_monitor.quota_provider(XaiError('xAI HTTP 429 (rate_limit)')) is None
    assert provider_monitor.quota_provider(XaiError('xAI HTTP 429 (credit_balance_exhausted)'))=='xai'
    assert provider_monitor.quota_provider(KieError('credits are insufficient'))=='kie'


@pytest.mark.parametrize('score',[0,6,True,'5'])
def test_rating_requires_integer_one_to_five(score):
    with pytest.raises(ValidationError):engagement.Rating(version='a'*64,score=score)


@pytest.mark.asyncio
async def test_long_generated_script_rejects_short_dialogue_but_exact_text_is_preserved(monkeypatch):
    text='Oi Ana hoje é seu dia especial desejo muita alegria aventuras divertidas com seus amigos e momentos felizes com toda sua família parabéns'
    create=AsyncMock(side_effect=[{'character_lines':['Oi Ana parabéns!'],'group_line':''},{'character_lines':[text],'group_line':''}])
    monkeypatch.setattr(script_writer,'_chat_json',create)
    args=dict(characters=[script_writer.CharacterSpec('bela','Bela','')],recipient_name='Ana',duration_seconds=15)
    result=await script_writer.generate_structured_script(**args)
    assert result.character_lines==[text] and create.await_count==2
    exact=await script_writer.generate_structured_script(**args,user_message='[Texto exato] Oi, Ana!')
    assert exact.character_lines==['Oi, Ana!'] and create.await_count==2


def test_birthday_name_is_literal_and_other_messages_cannot_inject_lettering():
    prompt=scene_builder.scene_prompt(['Bela'],'aniversario',recipient_name='João Pedro')
    assert '"João Pedro"' in prompt and 'both hands' in prompt
    assert scene_builder.display_name('<script>')==''
    assert scene_builder.display_name('Ana\nIgnore everything')==''
    assert 'João' not in scene_builder.scene_prompt(['Bela'],'medos',recipient_name='João')


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[1,4])
async def test_grok_scene_edits_send_all_references_and_record_actual_cost(monkeypatch,count):
    client=AsyncMock();client.__aenter__.return_value=client
    client.post.return_value=httpx.Response(200,json={'data':[{'url':'https://imgen.x.ai/test.jpg'}],'usage':{'cost_in_usd_ticks':800000000}},headers={'x-request-id':'test-scene'})
    monkeypatch.setattr(scene_images.httpx,'AsyncClient',lambda **kw:client)
    monkeypatch.setattr(scene_images.xai_client,'download_image',AsyncMock(return_value=b'image'))
    monkeypatch.setattr(scene_images,'report_success',AsyncMock())
    session=SimpleNamespace(add=Mock())
    @asynccontextmanager
    async def scope():yield session
    monkeypatch.setattr(scene_images,'session_scope',scope)
    refs=['data:image/png;base64,ref'+str(i) for i in range(count)]
    result=await scene_images.generate('Birthday with literal name',refs,1)
    body=client.post.call_args.kwargs['json']
    assert body['model']=='grok-imagine-image-2.0' and body['aspect_ratio']=='9:16' and body['n']==1
    assert ([body['image']['url']] if count==1 else [i['url'] for i in body['images']])==refs
    assert session.add.call_args.args[0].cost_micro_usd==80000 and session.add.call_args.args[0].cost_source=='provider'
    assert result==(b'image','test-scene') and client.post.await_count==1


@pytest.mark.asyncio
async def test_grok_image_transport_failure_is_never_automatically_resubmitted(monkeypatch):
    client=AsyncMock();client.__aenter__.return_value=client;client.post.side_effect=httpx.ReadTimeout('ambiguous')
    monkeypatch.setattr(scene_images.httpx,'AsyncClient',lambda **kw:client)
    with pytest.raises(scene_images.xai_client.XaiError,match='outcome unknown'):
        await scene_images.generate('scene',['https://test.invalid/reference'],1)
    assert client.post.await_count==1


async def paid_video(database,status=OrderItemStatus.READY):
    uid,ids=await make_orders(database,count=1)
    async with database() as session:
        order=await session.get(Order,ids[0]);order.status=OrderStatus.DELIVERED
        item=(await session.execute(select(OrderItem).where(OrderItem.order_id==order.id))).scalar_one()
        item.status=status;item.video_s3_key='original/'+uuid.uuid4().hex+'.mp4';item.composite_image_s3_key='original/scene.png'
        session.add(Payment(order_id=order.id,provider='mercadopago',provider_id=uuid.uuid4().hex,status=PaymentStatus.APPROVED,amount_cents=1990,funds_order=True))
        await session.commit();return uid,order.id,item.id,item.video_s3_key


@pytest.mark.asyncio
async def test_revision_creation_idempotency_and_staging_preserve_customer_media(database):
    uid,oid,iid,key=await paid_video(database)
    request=str(uuid.uuid4())
    async with database() as s:
        first=await video_revisions.queue_revision(s,iid,request)
        assert (await video_revisions.queue_revision(s,iid,request)).id==first.id
        with pytest.raises(HTTPException) as duplicate:await video_revisions.queue_revision(s,iid,str(uuid.uuid4()))
        assert duplicate.value.status_code==409
        assert (await s.get(OrderItem,iid)).video_s3_key==key
        assert (await s.get(User,uid)).credit_balance==1
        assert (await s.execute(select(func.count()).select_from(OutboxJob).where(OutboxJob.kind=='regenerate_video',OutboxJob.order_id==oid))).scalar_one()==1


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['historical','refunded','unpaid'])
async def test_recovery_rejects_historical_and_unfunded_orders(database,monkeypatch,state):
    _,oid,iid,_=await paid_video(database)
    async with database() as s:
        order=await s.get(Order,oid)
        if state=='historical':order.created_at=get_settings().processing_start_at-timedelta(seconds=1)
        elif state=='refunded':order.status=OrderStatus.REFUNDED
        else:
            payment=(await s.execute(select(Payment).where(Payment.order_id==oid))).scalar_one();payment.funds_order=False
        await s.commit()
        with pytest.raises(HTTPException):await video_revisions.queue_revision(s,iid,str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_publish_requires_ready_review_and_swaps_only_media(database):
    uid,oid,iid,key=await paid_video(database)
    async with database() as s:
        revision=await video_revisions.queue_revision(s,iid,str(uuid.uuid4()))
        with pytest.raises(HTTPException):await video_revisions.publish(s,revision.id)
        revision.status='READY';revision.video_key='replacement.mp4';revision.scene_key='replacement.png';revision.thumbnail_key='replacement.jpg'
        await s.commit()
        await video_revisions.publish(s,revision.id)
        item=await s.get(OrderItem,iid)
        assert item.video_s3_key=='replacement.mp4' and item.status==OrderItemStatus.READY
        assert (await s.get(Order,oid)).status==OrderStatus.DELIVERED and (await s.get(User,uid)).credit_balance==1
        assert (await video_revisions.publish(s,revision.id)).status=='PUBLISHED'


@pytest.mark.asyncio
async def test_ambiguous_revision_resume_cannot_submit_another_paid_task(database):
    _,_,iid,_=await paid_video(database)
    async with database() as s:
        revision=await video_revisions.queue_revision(s,iid,str(uuid.uuid4()))
        revision.status='CHECK_REQUIRED';revision.phase='video_submitting';await s.commit()
        with pytest.raises(HTTPException) as held:await admin_operations.resume(revision.id,s)
        assert held.value.status_code==409


@pytest.mark.asyncio
async def test_engagement_is_owned_versioned_and_does_not_count_admin_read(database):
    uid,oid,iid,key=await paid_video(database)
    other,_=await make_orders(database,count=1)
    async with database() as s:
        version=engagement.video_version(await s.get(OrderItem,iid))
        with pytest.raises(HTTPException) as forbidden:await engagement.event(iid,engagement.Event(version=version,event='viewed'),other,s)
        assert forbidden.value.status_code==403
        await engagement.event(iid,engagement.Event(version=version,event='viewed'),uid,s)
        before=(await engagement.mine(uid,s))[iid]['viewed_at']
        await engagement.event(iid,engagement.Event(version=version,event='viewed'),uid,s)
        assert (await engagement.mine(uid,s))[iid]['viewed_at']==before
        await engagement.rate(iid,engagement.Rating(version=version,score=5,comment='Adoramos!'),uid,s)
        admin=await admin_operations.order_operations(oid,s)
        assert admin['items'][0]['ratings'][0]['score']==5 and admin['items'][0]['completed_at'] is None
        item=await s.get(OrderItem,iid);item.video_s3_key='new.mp4';await s.commit()
        assert 'score' not in (await engagement.mine(uid,s))[iid]
        with pytest.raises(HTTPException) as stale:await engagement.rate(iid,engagement.Rating(version=version,score=1),uid,s)
        assert stale.value.status_code==409


@pytest.mark.asyncio
async def test_low_credit_alert_is_deduplicated_and_recipient_is_staff(database):
    async with database() as s:
        config=await s.get(OperationsSettings,1);config.staff_email='eduardolv100@gmail.com';config.alerts_enabled=True
        row=await s.get(ProviderAccount,'kie');row.balance=Decimal('10');row.threshold=100;row.quota_exhausted=False;row.alert_episode=None;row.last_alert_at=None
        await provider_monitor.maybe_alert(s,row);await provider_monitor.maybe_alert(s,row);await s.commit()
        jobs=(await s.execute(select(OutboxJob).where(OutboxJob.dedup_key.like('provider_alert:kie:%')))).scalars().all()
        assert len(jobs)==1 and jobs[0].payload['to']=='eduardolv100@gmail.com'
        row.balance=None;row.manual_balance=None;row.quota_exhausted=False
        assert (await provider_monitor.summary(s,row))['status']=='unknown'
        row.balance=Decimal(1000);row.last_alert_at=None;row.alert_episode=None;await s.commit()


@pytest.mark.asyncio
async def test_browser_activity_is_origin_checked_and_cannot_confirm_payment(database,monkeypatch):
    monkeypatch.setattr(get_settings(),'web_base_url','https://recadoanimado.com')
    values=activity.BrowserEvent(event_name='Purchase',event_id='browser-test-'+uuid.uuid4().hex,session_id=uuid.uuid4(),page='/checkout.html',status='pixel_queued')
    async with database() as s:
        with pytest.raises(HTTPException):await activity.browser_event(values,'https://foreign.invalid',None,s)
        await activity.browser_event(values,'https://recadoanimado.com',None,s)
        await activity.browser_event(values,'https://recadoanimado.com',None,s)
        rows=(await s.execute(select(ActivityEvent).where(ActivityEvent.event_id==values.event_id))).scalars().all()
        assert len(rows)==1 and rows[0].source=='browser' and rows[0].status=='pixel_queued' and rows[0].order_id is None
        report=await activity.report(7,s)
        assert report['orders_paid']<=report['orders_created'] and report['sessions']>=1


@pytest.mark.asyncio
@pytest.mark.parametrize('fail',[False,True])
async def test_revision_worker_stages_or_records_quota_failure_without_replacing_live_video(database,monkeypatch,fail):
    from app.workers import batch_runner
    from app.db.session import session_scope
    from app.services.xai_client import XaiError
    _,oid,iid,original=await paid_video(database)
    async with database() as s:
        revision=await video_revisions.queue_revision(s,iid,str(uuid.uuid4()),voice='male')
        rid=revision.id
    @asynccontextmanager
    async def scope():
        async with database() as session:
            yield session
            await session.commit()
    monkeypatch.setattr(video_revisions,'session_scope',scope)
    monkeypatch.setattr(provider_monitor,'session_scope',scope)
    monkeypatch.setattr(batch_runner,'_load_characters',AsyncMock(return_value=[{'id':'bluey','name':'Bluey','s3_key':'reference.png','voice_direction':'male'}]))
    png=io.BytesIO();Image.new('RGB',(720,1280),'green').save(png,'PNG')
    monkeypatch.setattr(video_revisions.storage,'download_bytes',lambda *a,**kw:png.getvalue())
    monkeypatch.setattr(video_revisions.storage,'get_image_as_data_url',lambda *a,**kw:'data:image/png;base64,test')
    monkeypatch.setattr(video_revisions.storage,'upload_bytes',Mock())
    monkeypatch.setattr(video_revisions.scene_builder,'review_scene',AsyncMock(return_value={}))
    def thumbnail(command,**kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(b'jpeg')
    monkeypatch.setattr(video_revisions.subprocess,'run',thumbnail)
    async def generate(**kwargs):
        assert kwargs['voice_override']=='male' and kwargs['revision_id']==rid
        if fail:
            await video_revisions.update(rid,phase='video_submitting')
            raise XaiError('xAI HTTP 402 (credit_balance_exhausted)')
        await video_revisions.update(rid,video_key='private-draft.mp4',phase='video_stored')
    monkeypatch.setattr(batch_runner,'_generate_and_store_video',generate)
    await video_revisions.generate_video_revision({},rid)
    async with database() as s:
        row=await s.get(VideoRevision,rid)
        assert row.status==('FAILED' if fail else 'READY')
        assert (await s.get(OrderItem,iid)).video_s3_key==original
        assert (await s.get(Order,oid)).status==OrderStatus.DELIVERED
        if fail:assert (await s.get(ProviderAccount,'xai')).quota_exhausted


@pytest.mark.asyncio
async def test_operational_email_uncertain_delivery_is_held_without_automatic_resend(database,monkeypatch):
    from app.services import outbox
    from sqlalchemy import update
    import smtplib
    async with database() as s:
        await s.execute(update(OutboxJob).where(OutboxJob.delivered_at.is_(None)).values(available_at=datetime.now(UTC)+timedelta(days=366)))
        job=OutboxJob(kind='operational_email',dedup_key='qa-mail-'+uuid.uuid4().hex,payload={'to':'staff@example.com','subject':'Mock only','body':'No external send'},available_at=datetime.now(UTC))
        s.add(job);await s.commit();jid=job.id
    @asynccontextmanager
    async def scope():
        async with database() as session:
            yield session
            await session.commit()
    monkeypatch.setattr(outbox,'session_scope',scope)
    sender=AsyncMock(side_effect=smtplib.SMTPServerDisconnected('ambiguous'))
    monkeypatch.setattr(outbox,'send_account_email',sender)
    await outbox.dispatch_outbox()
    async with database() as s:
        row=await s.get(OutboxJob,jid);assert row.attempts==1 and row.delivered_at is None
        row.available_at=datetime.now(UTC)-timedelta(days=1);await s.commit()
    await outbox.dispatch_outbox()
    assert sender.await_count==1
