from __future__ import annotations

import os

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "sqlite+pysqlite:///./test_black_soil_loop.sqlite3"
)
os.environ["JWT_SECRET"] = "test-secret-with-at-least-thirty-two-characters-2026"
os.environ["UPLOAD_DIR"] = "./test_uploads"

import pytest
from fastapi.testclient import TestClient

from app.b01.main import app as b01_app
from app.b02.main import app as b02_app
from app.seed import seed


@pytest.fixture(scope="session", autouse=True)
def seeded_database():
    seed(reset=True)
    yield


@pytest.fixture
def b01_client():
    with TestClient(b01_app) as client:
        yield client


@pytest.fixture
def b02_client():
    with TestClient(b02_app) as client:
        yield client


def login(client: TestClient, prefix: str, username: str) -> dict:
    response = client.post(
        f"{prefix}/auth/login",
        json={"username": username, "password": "Demo-Change-Me-2026"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def admin_headers(b01_client):
    body = login(b01_client, "/api/v1/web", "admin")
    return {"Authorization": f"Bearer {body['access_token']}"}
