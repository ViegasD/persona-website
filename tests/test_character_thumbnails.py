import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from botocore.exceptions import ClientError

from app.api.v1 import catalog
from app.db.session import get_session
from app.services import storage


@pytest.mark.parametrize('mode', ['RGB', 'RGBA', 'P'])
def test_thumbnail_is_small_valid_webp(monkeypatch, mode):
    image = Image.new(mode, (1200, 600))
    source = io.BytesIO()
    image.save(source, format='PNG')
    monkeypatch.setattr(storage, 'download_bytes', lambda key: source.getvalue())
    with Image.open(io.BytesIO(storage.thumbnail_bytes('characters/bluey/ref.png'))) as result:
        assert result.format == 'WEBP'
        assert result.size == (320, 160)


def client_for(session):
    app = FastAPI()
    app.include_router(catalog.router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_public_thumbnail_reads_only_catalog_key(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: 'characters/bluey/ref.png')))
    monkeypatch.setattr(storage, 'thumbnail_bytes', lambda key: b'webp-image' if key == 'characters/bluey/ref.png' else None)
    with client_for(session) as client:
        response = client.get('/catalog/characters/bluey/thumbnail')
        assert response.status_code == 200 and response.content == b'webp-image'
        assert response.headers['content-type'] == 'image/webp'
        assert response.headers['cache-control'] == 'public, max-age=3600'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert client.get('/catalog/characters/invalid_slug/thumbnail').status_code == 422
    query, params = session.execute.call_args.args
    assert params == {'slug': 'bluey'}
    assert 'is_active = true' in str(query)


@pytest.mark.parametrize('error,expected', [(None,404), ('missing',404), ('storage',503)])
def test_unavailable_thumbnail_does_not_expose_storage(monkeypatch,error,expected):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None if error is None else 'private-key')))
    def unavailable(key):
        raise ClientError({'Error':{'Code':'NoSuchKey' if error == 'missing' else 'AccessDenied'}},'GetObject')
    monkeypatch.setattr(storage,'thumbnail_bytes',unavailable)
    with client_for(session) as client:
        response=client.get('/catalog/characters/bluey/thumbnail')
        assert response.status_code == expected
        assert 'private-key' not in response.text
