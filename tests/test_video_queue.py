from unittest.mock import AsyncMock

import pytest
from arq.worker import get_kwargs
from pydantic import ValidationError

from app.core.settings import Settings, get_settings
from app.workers import queue
from app.workers.batch_runner import WorkerSettings


@pytest.mark.asyncio
async def test_producer_uses_selected_video_queue(monkeypatch):
    monkeypatch.setattr(get_settings(), 'video_queue_name', 'persona:video:new-only')
    monkeypatch.setattr(queue, '_pool', None)
    create = AsyncMock()
    monkeypatch.setattr(queue, 'create_pool', create)
    assert await queue.get_pool() is create.return_value
    create.assert_awaited_once_with(queue.redis_settings(), default_queue_name='persona:video:new-only')


def test_arq_receives_explicit_video_queue():
    assert get_kwargs(WorkerSettings)['queue_name'] == get_settings().video_queue_name


def test_video_queue_cannot_consume_account_queue():
    values = get_settings().model_dump(by_alias=True)
    values['STOREFRONT_VIDEO_QUEUE'] = 'persona:account:queue'
    with pytest.raises(ValidationError, match='must be separate'):
        Settings.model_validate(values)
