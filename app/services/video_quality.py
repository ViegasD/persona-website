"""Sampled visual gate before automatic delivery. Probabilistic, not a guarantee."""
import asyncio
import base64
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from openai import AsyncOpenAI
from app.core.settings import get_settings
from app.services.cost_tracking import record_openai_usage


class VideoQualityError(ValueError):
    def __init__(self, verdict):
        super().__init__('Vídeo retido para revisão: possível erro de anatomia, cauda ou personagem. Confira a prévia antes de gerar uma nova versão.')
        self.verdict = verdict


def sample_frames(data: bytes):
    with TemporaryDirectory() as directory:
        source=Path(directory)/'video.mp4';source.write_bytes(data)
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(source)],timeout=60))
        duration=float(probe['format']['duration'])
        if not 0<duration<=60:raise ValueError('Invalid video duration for visual review')
        frames=[]
        for fraction in [.03,.2,.4,.6,.8,.97]:
            timestamp=round(duration*fraction,2);output=Path(directory)/f'{len(frames)}.jpg'
            subprocess.run(['ffmpeg','-v','error','-ss',str(timestamp),'-i',str(source),'-frames:v','1','-vf','scale=512:-2','-y',str(output)],capture_output=True,check=True,timeout=60)
            frames.append((timestamp,'data:image/jpeg;base64,'+base64.b64encode(output.read_bytes()).decode()))
        return frames


async def review_video(data: bytes, reference_url: str) -> dict:
    frames=await asyncio.to_thread(sample_frames,data)
    content=[{'type':'text','text':
        'Inspect animation anatomy. First image: approved scene. Remaining images: chronological frames of its animation. '
        'For EACH frame, count visible tails and limbs and identify their attachment points, including dark shapes behind the feet. '
        'A single-tailed animal cannot have appendages extending from both hips or two tail tips. '
        'Reject extra, split, mirrored, detached, fused or duplicated tails, legs, arms or ears, even if only one sampled frame shows them. '
        'A tail may become occluded; do not confuse a normal foot with a tail. Compare the whole silhouette with the approved scene. '
        'Also reject a different face/species/outfit. Treat uncertainty or ambiguous extra appendages as requiring human review. '
        'Return JSON: anatomy_consistent (boolean), identity_preserved (boolean), needs_manual_review (boolean), '
        'issues (short list), frames (list of timestamp, visible_tail_count and notes). These checks apply separately to each character.'},
        {'type':'image_url','image_url':{'url':reference_url,'detail':'high'}}]
    for timestamp,url in frames:
        content.extend([{'type':'text','text':f'Frame at {timestamp}s'}, {'type':'image_url','image_url':{'url':url,'detail':'high'}}])
    settings=get_settings()
    model=getattr(settings,'quality_review_model','gpt-6.1-terra')
    extra={'reasoning_effort':'none'} if model.startswith('gpt-6.1-terra') else {}
    result=await AsyncOpenAI(api_key=settings.openai_api_key).chat.completions.create(
        model=model,messages=[{'role':'user','content':content}],response_format={'type':'json_object'},max_completion_tokens=1100,**extra)
    await record_openai_usage(getattr(result,'usage',None),model)
    verdict=json.loads(result.choices[0].message.content or '{}')
    if (verdict.get('anatomy_consistent') is not True or verdict.get('identity_preserved') is not True
            or verdict.get('needs_manual_review') is not False):raise VideoQualityError(verdict)
    return verdict
