from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import io
import json
import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.v1 import admin
from app.db.models import OrderStatus, OrderItemStatus
from app.db.session import get_session
from app.services import scene_builder


def test_single_character_scene_requires_full_background_and_reference_identity():
    prompt = scene_builder.scene_prompt(["Galinha Pintadinha"], "aniversario")
    assert "1 character" in prompt
    assert "birthday" in prompt and "flower garden" in prompt
    assert "strict identity references" in prompt and "checkerboard" in prompt
    assert "edge to edge" in prompt and "65-80" in prompt


@pytest.mark.parametrize("size,accepted", [((720,1280),True), ((640,640),False), ((144,256),False)])
def test_scene_rejects_wrong_aspect_or_thumbnail_resolution(size,accepted):
    out=io.BytesIO(); Image.new("RGB",size).save(out,"PNG")
    if accepted: scene_builder.validate_scene_dimensions(out.getvalue())
    else:
        with pytest.raises(ValueError): scene_builder.validate_scene_dimensions(out.getvalue())


def test_catalog_compositing_removes_only_connected_white_background():
    from PIL import ImageDraw
    source=Image.new('RGB',(100,150),'white');draw=ImageDraw.Draw(source)
    draw.rectangle((20,20,80,149),fill='blue');draw.rectangle((45,40,55,50),fill='white')
    original=io.BytesIO();source.save(original,'PNG')
    background=io.BytesIO();Image.new('RGB',(720,1280),'green').save(background,'PNG')
    result=Image.open(io.BytesIO(scene_builder.compose_original_on_background(original.getvalue(),background.getvalue())))
    assert result.size==(720,1280) and result.getpixel((0,0))==(0,128,0)
    colors=result.getcolors(720*1280)
    assert any(color==(0,0,255) for _,color in colors)
    assert any(color==(255,255,255) for _,color in colors)  # White details inside artwork survive.


def test_compositor_rejects_unsegmented_colored_background():
    source=io.BytesIO();Image.new('RGB',(100,150),'blue').save(source,'PNG')
    with pytest.raises(ValueError,match='manual cutout'):
        scene_builder.compose_original_on_background(source.getvalue(),source.getvalue())


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,background,prominence", [(False,True,True),(True,False,True),(True,True,False)])
async def test_bad_scene_cannot_reach_video_generation(monkeypatch,identity,background,prominence):
    verdict = {"identity_preserved":identity,"full_background":background,"prominent_subjects":prominence}
    create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(verdict)))]))
    monkeypatch.setattr(scene_builder,"AsyncOpenAI",lambda **kw:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(ValueError,match="scene failed"):
        await scene_builder.review_scene(["original"],"candidate")


def test_admin_order_videos_are_authenticated_signed_and_do_not_expose_storage():
    item=SimpleNamespace(id=1,sequence=1,status=OrderItemStatus.READY,character_ids=["bela"],
                         video_s3_key="private/video.mp4",composite_image_s3_key="private/scene.png",thumbnail_s3_key="private/thumb.jpg",custom_message=None,error=None)
    order=SimpleNamespace(id=177,status=OrderStatus.DELIVERED,recipient_name="Test",recipient_age=None,occasion_slug=None,
                          guest_email=None,guest_phone=None,created_at=None,paid_at=None,delivered_at=None,items=[item])
    session=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda:order)))
    app=FastAPI();app.include_router(admin.router);app.dependency_overrides[get_session]=lambda:session
    client=TestClient(app)
    assert client.get('/admin/orders/177/videos').status_code==401
    response=client.get('/admin/orders/177/videos',headers={'X-Api-Key':'admin-key'})
    assert response.status_code==200
    data=response.json()['items'][0]
    assert '/api/v1/media/' in data['video_url'] and '?scene=true' in data['scene_url']
    assert 'private/' not in response.text


@pytest.mark.asyncio
async def test_private_video_byte_ranges_and_invalid_ranges(monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi import HTTPException
    from app.api.v1 import media
    from app.core.security import sign_media_preview_token
    item=SimpleNamespace(id=1,character_ids=['bela'],video_s3_key='private/video.mp4',composite_image_s3_key=None)
    @asynccontextmanager
    async def session_scope():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda:item)))
    monkeypatch.setattr(media,'session_scope',session_scope)
    read=MagicMock(return_value=(b'0123','bytes 0-3/100'))
    monkeypatch.setattr(media.storage,'download_byte_range',read)
    token=sign_media_preview_token(1)
    response=await media.proxy_media(token,range_header='bytes=0-3')
    assert response.status_code==206 and response.body==b'0123'
    assert response.headers['content-range']=='bytes 0-3/100'
    assert response.headers['accept-ranges']=='bytes'
    for invalid in ['bytes=-','bytes=0-2,5-9','garbage']:
        with pytest.raises(HTTPException) as error:
            await media.proxy_media(token,range_header=invalid)
        assert error.value.status_code==416
    assert read.call_count==1
