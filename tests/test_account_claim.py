import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import credits, orders, payments
from app.api.v1.auth import router
from app.db.session import get_session


def test_guest_claim_cannot_issue_token_from_email_alone():
    app = FastAPI()
    app.include_router(router)
    async def no_database():
        yield None
    app.dependency_overrides[get_session] = no_database
    response = TestClient(app, raise_server_exceptions=False).post("/auth/guest-claim", json={
        "email": "victim@example.com", "code": "anything",
    })
    assert response.status_code == 401


@pytest.mark.parametrize("url", ["/credits/me", "/orders/mine", "/orders/lookup?email=victim@example.com", "/payments/1"])
def test_account_resources_reject_anonymous_access(url):
    app = FastAPI()
    for protected_router in [credits.router, orders.router, payments.router]:
        app.include_router(protected_router)
    async def no_database():
        yield None
    app.dependency_overrides[get_session] = no_database
    response = TestClient(app, raise_server_exceptions=False).get(url)
    assert response.status_code == 401
