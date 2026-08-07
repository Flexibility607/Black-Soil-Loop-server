from __future__ import annotations

from app.worker import process_pending_once
from tests.conftest import login


def test_confirmed_plan_cancellation_is_projected_to_b02(b01_client, b02_client):
    admin = login(b01_client, "/api/v1/web", "admin")
    headers = {"Authorization": f"Bearer {admin['access_token']}"}
    preview = b01_client.post(
        "/api/v1/web/algorithms/carpool/preview",
        json={"scenario_code": "SCENARIO_2", "order_ids": []},
        headers=headers,
    ).json()
    confirmed = b01_client.post(
        f"/api/v1/web/algorithms/carpool/runs/{preview['match_run_id']}/confirm",
        json={"match_run_id": preview["match_run_id"], "candidate_index": 0, "object_version": 1},
        headers=headers,
    )
    assert confirmed.status_code == 201, confirmed.text
    plan = confirmed.json()["plan"]
    process_pending_once()
    ready = b01_client.get(f"/api/v1/web/transport/plans/{plan['id']}", headers=headers).json()["plan"]
    cancelled = b01_client.post(
        f"/api/v1/web/transport/plans/{plan['id']}/cancel",
        json={"object_version": ready["object_version"], "reason": "门店临时闭店，取消本次配送"},
        headers={**headers, "Idempotency-Key": "cancel-plan-001"},
    )
    assert cancelled.status_code == 202, cancelled.text
    process_pending_once()
    final_plan = b01_client.get(f"/api/v1/web/transport/plans/{plan['id']}", headers=headers).json()["plan"]
    assert final_plan["status"] == "CANCELLED"

    mobile_admin = login(b02_client, "/api/v1/mobile", "admin")
    mobile_headers = {"Authorization": f"Bearer {mobile_admin['access_token']}"}
    task = b02_client.get(f"/api/v1/mobile/tasks/{ready['task_id']}", headers=mobile_headers)
    assert task.status_code == 200
    assert task.json()["task"]["status"] == "CANCELLED"
