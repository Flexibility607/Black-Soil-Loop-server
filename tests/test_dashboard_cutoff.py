from __future__ import annotations

from tests.conftest import login


def test_dashboard_cutoff_does_not_move_when_only_response_is_regenerated(b01_client):
    auth = login(b01_client, "/api/v1/web", "admin")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    first = b01_client.get("/api/v1/web/dashboard/snapshot", headers=headers)
    second = b01_client.get("/api/v1/web/dashboard/snapshot", headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data_cutoff"] == second.json()["data_cutoff"]
    assert first.json()["generated_at"] != first.json()["data_cutoff"]
    assert second.json()["generated_at"] != second.json()["data_cutoff"]
