from __future__ import annotations

from app.shared.models import IdempotencyRecord
from tests.conftest import login


def test_idempotency_scope_supports_composite_business_keys():
    assert IdempotencyRecord.__table__.c.scope.type.length >= 200


def test_single_device_session_and_refresh_cookie(b01_client):
    first = login(b01_client, "/api/v1/web", "enterprise_demo")
    second = login(b01_client, "/api/v1/web", "enterprise_demo")
    replaced = b01_client.get(
        "/api/v1/web/auth/me",
        headers={"Authorization": f"Bearer {first['access_token']}"},
    )
    assert replaced.status_code == 401
    assert replaced.json()["code"] in {"SESSION_INVALID", "SESSION_REPLACED"}
    current = b01_client.get(
        "/api/v1/web/auth/me",
        headers={"Authorization": f"Bearer {second['access_token']}"},
    )
    assert current.status_code == 200
    assert current.json()["idle_timeout_seconds"] == 1800


def test_openapi_contains_public_sse_mobile_ws_and_device_paths(b01_client, b02_client):
    b01_paths = b01_client.get("/openapi.json").json()["paths"]
    b02_paths = b02_client.get("/openapi.json").json()["paths"]
    assert "/api/v1/dashboard/events" in b01_paths
    assert "/api/v1/public/dashboard/snapshot" in b01_paths
    assert "/api/v1/web/assistant/transcriptions" in b01_paths
    assert "/api/v1/web/transport/plans/{plan_id}/publish" in b01_paths
    assert "/api/v1/mobile/tasks/{task_id}/receipts" in b02_paths
    assert "/api/v1/device/telemetry" in b02_paths


def test_all_errors_use_contract_envelope(b01_client):
    response = b01_client.get("/api/v1/web/transport/orders")
    assert response.status_code == 401
    body = response.json()
    assert set(["code", "message", "details", "trace_id"]).issubset(body)


def test_dashboard_units_deterministic_assistant_and_optional_transcription(b01_client, admin_headers):
    snapshot = b01_client.get("/api/v1/web/dashboard/snapshot", headers=admin_headers)
    assert snapshot.status_code == 200, snapshot.text
    body = snapshot.json()
    assert body["charts"]["daily_demand_by_unit"]
    assert {"date", "channel", "unit", "quantity"}.issubset(body["charts"]["daily_demand_by_unit"][0])
    assert body["summary"]["preorder_count"] >= 1

    answer = b01_client.post(
        "/api/v1/web/assistant/query",
        headers=admin_headers,
        json={"question": "第三空间营业额占比是多少？", "preferred_chart": "auto"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["mode"] == "deterministic"
    assert answer.json()["chart"]["type"] == "donut"

    transcription = b01_client.post(
        "/api/v1/web/assistant/transcriptions",
        headers=admin_headers,
        data={"duration_seconds": "1.2"},
        files={"audio": ("question.webm", b"not-a-real-recording", "audio/webm")},
    )
    assert transcription.status_code == 503
    assert transcription.json()["code"] == "TRANSCRIPTION_NOT_CONFIGURED"
