import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import video_quality,scene_builder
from app.services.prompt_builder import build_video_prompt,VideoPromptInputs,CharacterLine


def test_anatomy_rules_do_not_require_changing_the_character_pose():
    prompt=scene_builder.scene_prompt(['Bluey'],'aniversario',recipient_name='Sofia')
    assert 'exactly ONE' in prompt
    assert 'balanced speaking pose' not in prompt and 'feet or paws' not in prompt
    assert 'Remove raised-leg' not in prompt
    assert 'retain waist-up framing' in prompt


@pytest.mark.parametrize('count',[1,2,4])
def test_anatomy_lock_is_retained_for_single_and_multiple_speakers(count):
    prompt=build_video_prompt(VideoPromptInputs('Approved expressive scene',[CharacterLine('','Oi!') for _ in range(count)],'',15))
    assert 'ANATOMY LOCK' in prompt and 'only ONE tail' in prompt
    assert 'no leg lifting' not in prompt and 'expressive poses' in prompt
    assert 'same art style' in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('missing',['anatomy_correct'])
async def test_scene_reviewer_requires_unambiguous_anatomy(monkeypatch,missing):
    verdict={key:True for key in ['identity_preserved','full_background','prominent_subjects','anatomy_correct']}
    verdict[missing]=False
    call=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(verdict)))]))
    monkeypatch.setattr(scene_builder,'AsyncOpenAI',lambda **kwargs:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
    with pytest.raises(ValueError,match='scene failed'):await scene_builder.review_scene(['reference'],'scene')
    content=call.call_args.kwargs['messages'][0]['content']
    assert all(c['image_url']['detail']=='high' for c in content if c['type']=='image_url')


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict',[
    {'anatomy_consistent':False,'identity_preserved':True,'needs_manual_review':True,'issues':['two tails']},
    {'anatomy_consistent':True,'identity_preserved':True,'needs_manual_review':True},
    {'anatomy_consistent':True,'identity_preserved':True},
    {},
])
async def test_bad_or_uncertain_video_is_held(monkeypatch,verdict):
    call=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(verdict)))],usage=None))
    monkeypatch.setattr(video_quality,'AsyncOpenAI',lambda **kwargs:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
    monkeypatch.setattr(video_quality,'sample_frames',lambda data:[(3,'frame-with-two-tails')])
    with pytest.raises(video_quality.VideoQualityError) as result:
        await video_quality.review_video(b'video','approved-scene')
    assert result.value.verdict==verdict


@pytest.mark.asyncio
async def test_valid_video_has_one_review_and_preserves_frame_context(monkeypatch):
    verdict={'anatomy_consistent':True,'identity_preserved':True,'needs_manual_review':False,'issues':[]}
    call=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(verdict)))],usage=None))
    monkeypatch.setattr(video_quality,'AsyncOpenAI',lambda **kwargs:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
    monkeypatch.setattr(video_quality,'sample_frames',lambda data:[(3,'first'),(12,'last')])
    assert await video_quality.review_video(b'video','approved-scene')==verdict
    assert call.await_count==1
    content=call.call_args.kwargs['messages'][0]['content']
    assert [p['image_url']['url'] for p in content if p['type']=='image_url']==['approved-scene','first','last']
