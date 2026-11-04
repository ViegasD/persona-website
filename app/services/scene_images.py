"""One reference-guided Grok scene per request; no blind paid POST retries."""
from uuid import uuid4
from decimal import Decimal
import httpx
from app.core.settings import get_settings
from app.db.models import ApiCostLog
from app.db.session import session_scope
from app.services import xai_client
from app.services.provider_monitor import report_success
from app.services.finance import money

async def generate(prompt: str, references: list[str], item_id: int, *, before_submit=None) -> tuple[bytes,str]:
    if not 1<=len(references)<=5:raise ValueError('A scene requires one to five catalog references')
    settings=get_settings();model=settings.xai_image_model
    if model=='grok-imagine-image' and len(references)>3:raise ValueError('Use grok-imagine-image-2.0 for more than three references')
    body={'model':model,'prompt':prompt,'aspect_ratio':'9:16','n':1,'response_format':'url'}
    if len(references)==1:body['image']={'type':'image_url','url':references[0]}
    else:body['images']=[{'type':'image_url','url':url} for url in references]
    if model=='grok-imagine-image-2.0':body.update(resolution='1k',quality='medium')
    if before_submit:await before_submit()
    try:
        async with httpx.AsyncClient(timeout=240) as client:
            response=await client.post(settings.xai_api_base.rstrip('/')+'/images/edits',headers=xai_client._headers(),json=body)
    except httpx.RequestError:
        raise xai_client.XaiError('xAI image generation connection failed; outcome unknown') from None
    if response.status_code>=400:raise xai_client._response_error(response)
    result=xai_client._json_object(response)
    request_id=response.headers.get('x-request-id') or 'image-'+uuid4().hex
    # Record a completed billable API response before downloading or reviewing it.
    ticks=(result.get('usage') or {}).get('cost_in_usd_ticks')
    known=isinstance(ticks,int) and not isinstance(ticks,bool) and ticks>=0
    estimate=(60000+10000*len(references)) if model=='grok-imagine-image-2.0' else 20000+2000*len(references)
    async with session_scope() as session:
        session.add(ApiCostLog(order_item_id=item_id,provider='xai',operation='scene_image',model=model,provider_request_id=request_id,
            cost_micro_usd=money(Decimal(ticks)/10000) if known else estimate,cost_source='provider' if known else 'estimated',
            usage={'cost_in_usd_ticks':ticks} if known else {'reference_images':len(references),'resolution':'1k','quality':'medium'}))
    await report_success('xai')
    images=result.get('data')
    if not isinstance(images,list) or len(images)!=1 or not images[0].get('url'):
        raise xai_client.XaiError('xAI image returned no usable image; outcome unknown')
    return await xai_client.download_image(images[0]['url']),request_id
