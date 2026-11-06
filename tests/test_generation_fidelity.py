import io
from unittest.mock import AsyncMock
import pytest
from PIL import Image
from pydantic import ValidationError
from app.services import script_writer
from app.services.storage import prepare_portrait_reference
from app.services.prompt_builder import VideoPromptInputs, CharacterLine, build_video_prompt
from app.services.storefront_policy import PolicyValues


def test_portrait_reference_preserves_complete_landscape_image():
    source = Image.new("RGB", (640, 320), "blue")
    buf = io.BytesIO(); source.save(buf, "PNG")
    result = Image.open(io.BytesIO(prepare_portrait_reference(buf.getvalue())))
    assert result.size == (720, 1280)
    assert result.getpixel((40, 480)) == (0, 0, 255)
    assert result.getpixel((679, 799)) == (0, 0, 255)
    assert result.getpixel((0, 0)) == (255, 255, 255)


def test_multiple_characters_retain_identity_and_reference_pose_rules():
    prompt = build_video_prompt(VideoPromptInputs("Preserve the reference scene", [CharacterLine("left", "Oi, Ana!"), CharacterLine("right", "Parabéns!")], "", 8))
    assert "CRITICAL" in prompt and "REFERENCE IMAGE FIDELITY" in prompt
    assert "viewing angles" in prompt
    assert "natural body" not in prompt and "always facing directly" not in prompt
    assert "All characters say together" not in prompt


@pytest.mark.asyncio
async def test_dialogue_retries_oversized_output_with_shared_budget(monkeypatch):
    call = AsyncMock(side_effect=[{"character_lines": ["long " * 30], "group_line": ""}, {"character_lines": ["Oi, Ana! Feliz aniversário!"], "group_line": ""}])
    monkeypatch.setattr(script_writer, "_chat_json", call)
    script = await script_writer.generate_structured_script(characters=[script_writer.CharacterSpec("bela", "Bela", "")], recipient_name="Ana", duration_seconds=8)
    assert call.await_count == 2
    assert script_writer.script_fits(script, 8) and script.group_line == ""


@pytest.mark.asyncio
async def test_fear_occasion_is_not_replaced_by_generic_greeting(monkeypatch):
    import json
    call = AsyncMock(return_value={"character_lines": ["Oi, Ana! Você é corajosa e está segura!"], "group_line": ""})
    monkeypatch.setattr(script_writer, "_chat_json", call)
    await script_writer.generate_structured_script(characters=[script_writer.CharacterSpec("a", "A", "")], recipient_name="Ana", occasion_slug="medos")
    assert "medos" in json.loads(call.call_args.args[0][1]["content"])["ocasiao"]


@pytest.mark.asyncio
async def test_oversized_dialogue_never_silently_generates_generic_content(monkeypatch):
    monkeypatch.setattr(script_writer, "_chat_json", AsyncMock(return_value={"character_lines": ["word " * 40], "group_line": ""}))
    with pytest.raises(RuntimeError, match="duration"):
        await script_writer.generate_structured_script(characters=[script_writer.CharacterSpec("a", "A", "")], recipient_name="Ana", duration_seconds=8)


@pytest.mark.asyncio
async def test_exact_multi_speaker_text_preserves_language_and_rejects_excess_before_api(monkeypatch):
    call=AsyncMock();monkeypatch.setattr(script_writer,'_chat_json',call)
    characters=[script_writer.CharacterSpec('bluey','Bluey',''),script_writer.CharacterSpec('bingo','Bingo','')]
    text='Hey Liam, happy birthday! Play games with Melissa. Hooray!'
    result=await script_writer.generate_structured_script(characters=characters,recipient_name='Liam',user_message='[Texto exato] '+text,duration_seconds=15)
    assert ' '.join(result.character_lines)==text and len(result.character_lines)==2
    with pytest.raises(ValueError,match='Shorten'):
        await script_writer.generate_structured_script(characters=characters,recipient_name='Liam',user_message='[Texto exato] '+'word '*37,duration_seconds=15)
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_speaker_retry_includes_rejected_output_and_actual_shared_count(monkeypatch):
    rejected={'character_lines':['hello '*20,'hello '*20],'group_line':''}
    valid={'character_lines':['word '*12,'word '*12],'group_line':''}
    snapshots=[]
    async def reply(messages):
        snapshots.append([dict(m) for m in messages]);return rejected if len(snapshots)==1 else valid
    monkeypatch.setattr(script_writer,'_chat_json',reply)
    result=await script_writer.generate_structured_script(characters=[script_writer.CharacterSpec('a','A',''),script_writer.CharacterSpec('b','B','')],recipient_name='Liam',duration_seconds=15)
    assert script_writer.script_fits(result,15)
    assert snapshots[1][-2]['role']=='assistant' and '40 palavras' in snapshots[1][-1]['content']


@pytest.mark.parametrize("values", [{"video_duration_seconds": 16}, {"video_duration_seconds": 3}, {"video_model": "arbitrary-model"}])
def test_admin_generation_controls_reject_invalid_values(values):
    with pytest.raises(ValidationError):
        PolicyValues(**values)


@pytest.mark.asyncio
async def test_guest_order_read_requires_verified_matching_email_and_never_allows_edits():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from app.api.v1.orders import _load_order
    order = SimpleNamespace(id=177, user_id=None, guest_email="Buyer@Example.com")
    user = SimpleNamespace(email="buyer@example.com", email_verified_at=None)
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: order)), get=AsyncMock(return_value=user))
    with pytest.raises(HTTPException):
        await _load_order(session, 177, user_id=5, guest_order_ids=[])
    user.email_verified_at = object()
    assert await _load_order(session, 177, user_id=5, guest_order_ids=[]) is order
    with pytest.raises(HTTPException):
        await _load_order(session, 177, user_id=5, guest_order_ids=[], lock=True)
    user.email = "other@example.com"
    with pytest.raises(HTTPException):
        await _load_order(session, 177, user_id=5, guest_order_ids=[])
    user.email = "buyer@example.com"; order.user_id = 7
    with pytest.raises(HTTPException):
        await _load_order(session, 177, user_id=5, guest_order_ids=[])
