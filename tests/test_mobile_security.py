from __future__ import annotations

from sqlalchemy import select

from app.shared.database import SessionLocal
from app.shared.models import Product
from tests.conftest import login


def bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_web_cookie_refresh_requires_csrf(b01_client):
    body = login(b01_client, "/api/v1/web", "admin")
    rejected = b01_client.post("/api/v1/web/auth/refresh", json={})
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "CSRF_VALIDATION_FAILED"
    refreshed = b01_client.post(
        "/api/v1/web/auth/refresh",
        json={},
        headers={"X-CSRF-Token": body["csrf_token"]},
    )
    assert refreshed.status_code == 200, refreshed.text


def test_upload_magic_type_and_owner_access(b02_client):
    manager = login(b02_client, "/api/v1/mobile", "manager_s001")
    headers = bearer(manager)
    invalid = b02_client.post(
        "/api/v1/mobile/uploads?purpose=receipt",
        files={"file": ("fake.png", b"not-an-image", "image/png")},
        headers=headers,
    )
    assert invalid.status_code == 415
    assert invalid.json()["code"] == "UPLOAD_CONTENT_INVALID"

    png = b"\x89PNG\r\n\x1a\n" + b"demo-image-bytes"
    uploaded = b02_client.post(
        "/api/v1/mobile/uploads?purpose=receipt",
        files={"file": ("receipt.png", png, "image/png")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    attachment_id = uploaded.json()["attachment"]["id"]
    downloaded = b02_client.get(f"/api/v1/mobile/uploads/{attachment_id}", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == png

    other = login(b02_client, "/api/v1/mobile", "manager_s002")
    forbidden = b02_client.get(f"/api/v1/mobile/uploads/{attachment_id}", headers=bearer(other))
    assert forbidden.status_code == 403


def test_stockout_idempotency_conflict(b02_client):
    manager = login(b02_client, "/api/v1/mobile", "manager_t001")
    headers = {**bearer(manager), "Idempotency-Key": "stockout-security-001"}
    with SessionLocal() as db:
        product_id = db.scalar(select(Product.id).limit(1))
    first = b02_client.post(
        "/api/v1/mobile/stockouts",
        json={"product_id": product_id, "requested_quantity": 20, "reason": "库存不足"},
        headers=headers,
    )
    assert first.status_code == 201
    conflict = b02_client.post(
        "/api/v1/mobile/stockouts",
        json={"product_id": product_id, "requested_quantity": 40, "reason": "库存不足"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_driver_exception_preserves_original_status(b02_client):
    driver = login(b02_client, "/api/v1/mobile", "driver_demo")
    headers = bearer(driver)
    tasks = b02_client.get("/api/v1/mobile/tasks", headers=headers).json()["items"]
    active = next(item for item in tasks if item["status"] == "IN_TRANSIT")
    reported = b02_client.post(
        f"/api/v1/mobile/tasks/{active['id']}/exceptions",
        json={"exception_type": "DELAY", "reason": "道路临时管制，预计延误二十分钟"},
        headers={**headers, "Idempotency-Key": "driver-exception-001"},
    )
    assert reported.status_code == 201, reported.text
    assert reported.json()["original_status"] == "IN_TRANSIT"
    current = b02_client.get(f"/api/v1/mobile/tasks/{active['id']}", headers=headers)
    assert current.json()["task"]["status"] == "IN_TRANSIT"
