from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.shared.database import SessionLocal
from app.shared.models import DashboardProjection
from app.shared.periods import period_window
from tests.conftest import login


def test_period_window_uses_shanghai_natural_days():
    now = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)
    seven = period_window("7d", now)
    month = period_window("month", now)
    assert seven.start_date.isoformat() == "2026-08-03"
    assert seven.end_date.isoformat() == "2026-08-09"
    assert seven.start_at.isoformat() == "2026-08-02T16:00:00+00:00"
    assert seven.end_at.isoformat() == "2026-08-09T16:00:00+00:00"
    assert month.start_date.isoformat() == "2026-08-01"


def test_dashboard_period_changes_server_query_and_projection_key(b01_client):
    auth = login(b01_client, "/api/v1/web", "admin")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    responses = {
        period: b01_client.get(f"/api/v1/web/dashboard/snapshot?period={period}", headers=headers)
        for period in ("7d", "30d", "month")
    }
    for period, response in responses.items():
        assert response.status_code == 200, response.text
        assert response.json()["period"] == period

    seven = responses["7d"].json()
    thirty = responses["30d"].json()
    assert len(seven["charts"]["daily_operations"]) == 7
    assert len(thirty["charts"]["daily_operations"]) == 14
    assert seven["summary"]["total_sales_amount"] < thirty["summary"]["total_sales_amount"]

    with SessionLocal() as db:
        keys = set(
            db.scalars(
                select(DashboardProjection.id).where(
                    DashboardProjection.id.in_(("current:7d", "current:30d", "current:month"))
                )
            )
        )
    assert keys == {"current:7d", "current:30d", "current:month"}

    invalid = b01_client.get("/api/v1/web/dashboard/snapshot?period=90d", headers=headers)
    assert invalid.status_code == 422
