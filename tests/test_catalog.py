"""Public catalog contracts without external database or storage services."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.v1 import catalog
from app.db.session import get_session
from app.services import storage


@pytest.mark.asyncio
async def test_character_view_preserves_uuid_slug_tags_and_thumbnail(monkeypatch):
    row = dict(id="ed0c0113-e8de-45db-9e51-ed550fe457c7", name="Bluey",
               slug="bluey", description=None, thumbnail_key="characters/bluey/photo.webp",
               tags=["Bluey", "aniversario"], gender="female", age_range="child")
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        mappings=lambda: SimpleNamespace(all=lambda: [row]))))
    monkeypatch.setattr(storage, "presigned_get_url", lambda key, expires_in: "https://images.test/" + key)

    result = await catalog.list_characters(occasion="aniversario", gender="female",
                                           limit=200, offset=200, session=session)

    assert result[0].id == row["id"]
    assert result[0].slug == "bluey"
    assert result[0].tags == row["tags"]
    assert result[0].thumbnail_url == "https://images.test/characters/bluey/photo.webp"
    statement, params = session.execute.call_args.args
    assert "thumbnail_s3_key AS thumbnail_key" in str(statement)
    assert "age_range" in str(statement)
    assert "is_active = true" in str(statement)
    assert "ORDER BY id ASC" in str(statement)
    assert params == dict(limit=200, offset=200, occasion="aniversario", gender="female")


@pytest.mark.asyncio
async def test_character_database_failure_returns_retryable_error():
    session = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("database offline")))
    with pytest.raises(HTTPException) as error:
        await catalog.list_characters(occasion=None, gender=None, limit=200, offset=0, session=session)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_plans_filter_active_and_keep_catalog_order():
    plans = [SimpleNamespace(slug="new-plan", price_cents=2345, video_count=2)]
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: plans))))
    assert await catalog.list_plans(session=session) == plans
    statement = str(session.execute.call_args.args[0])
    assert "is_active IS true" in statement
    assert "ORDER BY web.plan.sort_order, web.plan.id" in statement


def test_character_http_response_supports_uuid_and_pagination_bounds(monkeypatch):
    row = dict(id="ed0c0113-e8de-45db-9e51-ed550fe457c7", name="Bluey", slug="bluey",
               thumbnail_key="characters/bluey/photo.webp", tags=["Familia"])
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        mappings=lambda: SimpleNamespace(all=lambda: [row]))))
    monkeypatch.setattr(storage, "presigned_get_url", lambda key, expires_in: "https://images.test/" + key)
    app = FastAPI()
    app.include_router(catalog.router)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        response = client.get("/catalog/characters?limit=200&offset=200")
        assert response.status_code == 200
        assert response.json()[0]["id"] == row["id"]
        assert response.json()[0]["thumbnail_url"] == "https://images.test/characters/bluey/photo.webp"
        assert response.json()[0]["tags"] == ["Familia"]
        assert client.get("/catalog/characters?limit=201").status_code == 422
        assert client.get("/catalog/characters?offset=-1").status_code == 422
