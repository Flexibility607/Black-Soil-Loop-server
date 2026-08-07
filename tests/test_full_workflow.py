from __future__ import annotations

from datetime import UTC, datetime

from app.shared.config import get_settings
from app.worker import process_pending_once
from tests.conftest import login


def bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_web_to_mobile_receipt_inventory_alert_and_dashboard(b01_client, b02_client):
    admin = login(b01_client, "/api/v1/web", "admin")
    admin_headers = bearer(admin)
    driver = login(b02_client, "/api/v1/mobile", "driver_demo")
    driver_headers = bearer(driver)
    driver_id = driver["user"]["driver_id"]

    preview = b01_client.post(
        "/api/v1/web/algorithms/carpool/preview",
        json={"scenario_code": "SCENARIO_1", "order_ids": []},
        headers=admin_headers,
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    candidate_index = next(
        index for index, candidate in enumerate(preview_body["candidates"]) if candidate["driver_id"] == driver_id
    )
    candidate = preview_body["candidates"][candidate_index]

    confirmed = b01_client.post(
        f"/api/v1/web/algorithms/carpool/runs/{preview_body['match_run_id']}/confirm",
        json={
            "match_run_id": preview_body["match_run_id"],
            "candidate_index": candidate_index,
            "object_version": 1,
        },
        headers=admin_headers,
    )
    assert confirmed.status_code == 201, confirmed.text
    plan = confirmed.json()["plan"]
    assert plan["status"] == "CONFIRMED"
    process_pending_once()
    ready_plan = b01_client.get(f"/api/v1/web/transport/plans/{plan['id']}", headers=admin_headers).json()["plan"]
    assert ready_plan["status"] == "READY"

    published = b01_client.post(
        f"/api/v1/web/transport/plans/{plan['id']}/publish",
        json={"object_version": ready_plan["object_version"]},
        headers={**admin_headers, "Idempotency-Key": "publish-flow-001"},
    )
    assert published.status_code == 202, published.text
    process_pending_once()
    published_plan = b01_client.get(f"/api/v1/web/transport/plans/{plan['id']}", headers=admin_headers).json()["plan"]
    assert published_plan["status"] == "PUBLISHED"
    qr_token = published_plan["published_qr_token"]
    task = {"id": published_plan["task_id"]}

    task_list = b02_client.get("/api/v1/mobile/tasks", headers=driver_headers)
    assert task_list.status_code == 200
    assert task["id"] in {item["id"] for item in task_list.json()["items"]}
    version = next(item["object_version"] for item in task_list.json()["items"] if item["id"] == task["id"])

    def action(name: str, key: str, *, stop_id: str | None = None, qr: bool = False):
        nonlocal version
        payload = {"action": name, "object_version": version}
        if stop_id:
            payload["stop_id"] = stop_id
        if qr:
            payload["qr_token"] = qr_token
        response = b02_client.post(
            f"/api/v1/mobile/tasks/{task['id']}/actions",
            json=payload,
            headers={**driver_headers, "Idempotency-Key": key},
        )
        assert response.status_code == 200, response.text
        version = response.json()["object_version"]
        return response

    action("ACCEPT", "flow-accept")
    conflict = b02_client.post(
        f"/api/v1/mobile/tasks/{task['id']}/actions",
        json={"action": "PICKUP", "object_version": 1, "qr_token": qr_token},
        headers={**driver_headers, "Idempotency-Key": "flow-stale"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "VERSION_CONFLICT"
    action("PICKUP", "flow-pickup", qr=True)
    action("START_TRANSIT", "flow-start")

    location = b02_client.post(
        f"/api/v1/mobile/tasks/{task['id']}/locations",
        json={
            "latitude": 45.741,
            "longitude": 126.619,
            "speed_mps": 12.4,
            "accuracy_m": 8.0,
            "recorded_at": datetime.now(UTC).isoformat(),
        },
        headers={**driver_headers, "Idempotency-Key": "flow-location-001"},
    )
    assert location.status_code == 201, location.text

    detail = b02_client.get(f"/api/v1/mobile/tasks/{task['id']}", headers=driver_headers).json()["task"]
    for index, stop in enumerate(detail["stops"], 1):
        action("DELIVER", f"flow-deliver-{index}", stop_id=stop["id"], qr=True)
    assert (
        b02_client.get(f"/api/v1/mobile/tasks/{task['id']}", headers=driver_headers).json()["task"]["status"]
        == "DELIVERED"
    )

    telemetry_payload = {
        "task_id": task["id"],
        "vehicle_id": candidate["vehicle_id"],
        "samples": [
            {
                "sampled_at": datetime.now(UTC).isoformat(),
                "temperature_c": 12.2,
                "humidity_pct": 92.0,
                "latitude": 45.74,
                "longitude": 126.62,
            }
        ],
    }
    telemetry_headers = {
        "X-Device-Key": get_settings().device_api_key,
        "Idempotency-Key": "flow-telemetry",
    }
    telemetry = b02_client.post("/api/v1/device/telemetry", json=telemetry_payload, headers=telemetry_headers)
    assert telemetry.status_code == 202, telemetry.text
    assert telemetry.json()["anomaly_types"] == ["HUMIDITY", "TEMPERATURE"]
    telemetry_retry = b02_client.post("/api/v1/device/telemetry", json=telemetry_payload, headers=telemetry_headers)
    assert telemetry_retry.status_code == 202
    assert telemetry_retry.json()["accepted"] == 1

    for index, stop in enumerate(detail["stops"], 1):
        store_resources = b01_client.get("/api/v1/web/master-data/stores", headers=admin_headers).json()["items"]
        store_code = next(item["code"] for item in store_resources if item["id"] == stop["store_id"])
        manager = login(b02_client, "/api/v1/mobile", f"manager_{store_code.lower()}")
        receipt = b02_client.post(
            f"/api/v1/mobile/tasks/{task['id']}/receipts",
            json={
                "object_version": version,
                "receipt_status": "FULL",
                "qr_token": qr_token,
                "lines": [
                    {
                        "product_id": line["product_id"],
                        "expected_quantity": line["expected_quantity"],
                        "received_quantity": line["expected_quantity"],
                        "note": None,
                    }
                    for line in stop["delivery_lines"]
                ],
            },
            headers={**bearer(manager), "Idempotency-Key": f"flow-receipt-{index}"},
        )
        assert receipt.status_code == 201, receipt.text
        version = receipt.json()["object_version"]

    final_detail = b02_client.get(f"/api/v1/mobile/tasks/{task['id']}", headers=driver_headers).json()["task"]
    assert final_detail["status"] == "COMPLETED"

    dashboard = b01_client.get("/api/v1/web/dashboard/snapshot", headers=admin_headers)
    assert dashboard.status_code == 200, dashboard.text
    dashboard_body = dashboard.json()
    assert dashboard_body["summary"]["third_space_count"] == 6
    assert dashboard_body["summary"]["open_alert_count"] >= 2
    assert dashboard_body["data_cutoff"]
    route = next(route for route in dashboard_body["map"]["routes"] if route["task_id"] == task["id"])
    assert route["latest_location"]["speed_mps"] == 12.4
