from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from app.b02 import main as b02_main
from app.shared.config import get_settings
from app.shared.database import SessionLocal
from app.shared.models import LocationPoint, TelemetryPoint, TransportTask, UserSession, utcnow
from app.shared.security import decode_token
from app.worker import process_pending_once
from tests.conftest import login


def _bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_websocket_session_check_covers_replacement_timeout_and_expiry(b02_client):
    auth = login(b02_client, "/api/v1/mobile", "driver_demo")
    payload = decode_token(auth["access_token"], "access")
    assert b02_main.WS_SESSION_CHECK_INTERVAL_SECONDS == 30
    with SessionLocal() as db:
        session = db.get(UserSession, payload["sid"])
        assert session is not None
        assert b02_main.websocket_session_error(db, payload) is None

        expired_payload = {**payload, "exp": utcnow().timestamp() - 1}
        assert b02_main.websocket_session_error(db, expired_payload) == "TOKEN_EXPIRED"

        session.last_activity_at = utcnow() - timedelta(minutes=get_settings().idle_timeout_minutes + 1)
        assert b02_main.websocket_session_error(db, payload) == "SESSION_TIMEOUT"

        session.last_activity_at = utcnow()
        session.expires_at = utcnow() - timedelta(seconds=1)
        assert b02_main.websocket_session_error(db, payload) == "SESSION_EXPIRED"

        session.expires_at = utcnow() + timedelta(days=1)
        session.revoked_at = utcnow()
        assert b02_main.websocket_session_error(db, payload) == "SESSION_REVOKED"
        db.rollback()


def test_websocket_closes_after_session_revocation(b02_client, monkeypatch):
    auth = login(b02_client, "/api/v1/mobile", "driver_demo")
    payload = decode_token(auth["access_token"], "access")
    monkeypatch.setattr(b02_main, "WS_SESSION_CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(b02_main, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    with b02_client.websocket_connect(
        f"/api/v1/mobile/ws?token={auth['access_token']}&cursor=2147483647"
    ) as websocket:
        heartbeat = websocket.receive_json()
        assert heartbeat["type"] == "heartbeat"
        with SessionLocal() as db:
            session = db.get(UserSession, payload["sid"])
            assert session is not None
            session.revoked_at = utcnow()
            db.commit()
        for _ in range(20):
            closed = websocket.receive()
            if closed["type"] == "websocket.close":
                break
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 4401
        assert closed["reason"] == "SESSION_REVOKED"


def test_location_and_telemetry_state_guards_are_projected(b01_client, b02_client):
    suffix = uuid4().hex[:10]
    driver = login(b02_client, "/api/v1/mobile", "driver_demo")
    driver_headers = _bearer(driver)
    with SessionLocal() as db:
        template = db.scalar(select(TransportTask).where(TransportTask.driver_id == driver["user"]["driver_id"]))
        assert template is not None
        task = TransportTask(
            task_no=f"SESSION-TELEMETRY-{suffix}",
            source_order_ids=[],
            vehicle_id=template.vehicle_id,
            driver_id=template.driver_id,
            store_id=template.store_id,
            status="PUBLISHED",
            temperature_zone=template.temperature_zone,
            total_weight_kg=1,
            total_volume_m3=0.1,
            origin_latitude=template.origin_latitude,
            origin_longitude=template.origin_longitude,
            destination_latitude=template.destination_latitude,
            destination_longitude=template.destination_longitude,
            planned_departure_at=utcnow(),
        )
        db.add(task)
        db.commit()
        task_id = task.id
        vehicle_id = task.vehicle_id

    location_body = {
        "latitude": 45.741,
        "longitude": 126.619,
        "speed_mps": 8.2,
        "accuracy_m": 6.0,
        "recorded_at": utcnow().isoformat(),
    }
    telemetry_body = {
        "task_id": task_id,
        "vehicle_id": vehicle_id,
        "samples": [
            {
                "sampled_at": utcnow().isoformat(),
                "temperature_c": 15.0,
                "humidity_pct": 96.0,
                "latitude": 45.741,
                "longitude": 126.619,
            }
        ],
    }
    device_headers = {"X-Device-Key": get_settings().device_api_key}

    rejected_location = b02_client.post(
        f"/api/v1/mobile/tasks/{task_id}/locations",
        json=location_body,
        headers={**driver_headers, "Idempotency-Key": f"location-published-{suffix}"},
    )
    assert rejected_location.status_code == 409
    assert rejected_location.json()["code"] == "LOCATION_TASK_STATE_INVALID"
    rejected_telemetry = b02_client.post(
        "/api/v1/device/telemetry",
        json=telemetry_body,
        headers={**device_headers, "Idempotency-Key": f"telemetry-published-{suffix}"},
    )
    assert rejected_telemetry.status_code == 409
    assert rejected_telemetry.json()["code"] == "TELEMETRY_TASK_STATE_INVALID"

    with SessionLocal() as db:
        task = db.get(TransportTask, task_id)
        assert task is not None
        task.status = "IN_TRANSIT"
        db.commit()

    accepted_location = b02_client.post(
        f"/api/v1/mobile/tasks/{task_id}/locations",
        json=location_body,
        headers={**driver_headers, "Idempotency-Key": f"location-transit-{suffix}"},
    )
    assert accepted_location.status_code == 201, accepted_location.text
    accepted_telemetry = b02_client.post(
        "/api/v1/device/telemetry",
        json=telemetry_body,
        headers={**device_headers, "Idempotency-Key": f"telemetry-transit-{suffix}"},
    )
    assert accepted_telemetry.status_code == 202, accepted_telemetry.text
    assert accepted_telemetry.json()["anomaly_types"] == ["HUMIDITY", "TEMPERATURE"]

    with SessionLocal() as db:
        task = db.get(TransportTask, task_id)
        assert task is not None
        task.status = "COMPLETED"
        db.commit()

    rejected_after_completion = b02_client.post(
        "/api/v1/device/telemetry",
        json=telemetry_body,
        headers={**device_headers, "Idempotency-Key": f"telemetry-completed-{suffix}"},
    )
    assert rejected_after_completion.status_code == 409
    assert rejected_after_completion.json()["code"] == "TELEMETRY_TASK_STATE_INVALID"

    while process_pending_once():
        pass
    admin = login(b01_client, "/api/v1/web", "admin")
    projected = b01_client.get(
        "/api/v1/web/telemetry-issues?period=30d&page_size=100",
        headers=_bearer(admin),
    )
    assert projected.status_code == 200, projected.text
    task_issues = [item for item in projected.json()["items"] if item["task_id"] == task_id]
    issue_types = {item["issue_type"] for item in task_issues}
    assert {
        "ANOMALOUS_TELEMETRY",
        "LOCATION_TASK_STATE_MISMATCH",
        "TELEMETRY_TASK_STATE_MISMATCH",
    }.issubset(issue_types)
    assert all(item["issue_type_label"] for item in task_issues)
    assert projected.json()["data_cutoff"]

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LocationPoint).where(LocationPoint.task_id == task_id)) == 1
        assert db.scalar(select(func.count()).select_from(TelemetryPoint).where(TelemetryPoint.task_id == task_id)) == 1
