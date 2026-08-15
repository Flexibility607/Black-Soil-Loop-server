from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.b01.main as b01_main
import app.domain.fixed_demo as fixed_demo
import app.worker as worker
from app.b01.main import app as b01_app
from app.b02.main import app as b02_app
from app.shared.config import get_settings
from app.shared.database import ShowcaseSessionLocal
from app.shared.models import DemoCaseInstallation, OutboxEvent, TransportPlan

pytestmark = pytest.mark.skipif(
    not os.environ.get("SHOWCASE_WORKER_DATABASE_URL", "").startswith("postgresql")
    or not os.environ.get("SHOWCASE_TEST_PASSWORD"),
    reason="需要已安装固定案例的隔离 PostgreSQL 演示库",
)


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    """Keep the installed showcase dataset intact instead of running the generic seed reset."""
    yield


def _process_showcase_events() -> int:
    processed = 0
    deadline = time.monotonic() + 2
    for _ in range(40):
        count = worker.process_pending_once(limit=1000)
        processed += count
        if count == 0 and processed:
            break
        if count == 0:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return processed


def test_postgres_showcase_login_refresh_snapshot_and_mobile_websocket(monkeypatch):
    production_settings = get_settings().model_copy(update={"environment": "production"})
    monkeypatch.setattr(b01_main, "get_settings", lambda: production_settings)
    password = os.environ["SHOWCASE_TEST_PASSWORD"]

    with TestClient(b01_app, base_url="https://testserver") as client:
        live_login = client.post(
            "/api/v1/web/auth/login?dataset=showcase",
            json={"username": "admin", "password": "Demo-Change-Me-2026"},
            headers={"X-Dataset": "showcase"},
        )
        assert live_login.status_code == 200
        assert live_login.json()["dataset_mode"] == "live"

        login = client.post(
            "/api/v1/web/auth/login",
            json={"username": "showcase-admin", "password": password},
        )
        assert login.status_code == 200
        body = login.json()
        assert body["dataset_mode"] == "showcase"
        assert body["case_version"] == "2026-08-15.1"
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        me = client.get("/api/v1/web/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["case_revision"] == body["case_revision"]
        snapshot = client.get("/api/v1/public/dashboard/snapshot?period=7d")
        assert snapshot.status_code == 200
        assert snapshot.json()["dataset_mode"] == "showcase"
        public_map = snapshot.json()["map"]
        assert public_map["active_routes"] == []
        assert "stores" not in public_map
        assert "routes" not in public_map
        csrf = client.cookies.get("blacksoil_csrf")
        refreshed = client.post(
            "/api/v1/web/auth/refresh",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["dataset_mode"] == "showcase"

    with TestClient(b02_app, base_url="https://testserver") as client:
        login = client.post(
            "/api/v1/mobile/auth/login",
            json={"username": "showcase-driver", "password": password},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        tasks = client.get(
            "/api/v1/mobile/tasks",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert tasks.status_code == 200
        with client.websocket_connect(f"/api/v1/mobile/ws?token={token}&cursor=999999") as websocket:
            assert websocket.receive_json()["type"] == "heartbeat"


def test_postgres_showcase_reset_keeps_projection_and_invalidates_session(monkeypatch):
    production_settings = get_settings().model_copy(update={"environment": "production"})
    showcase_worker_settings = production_settings.model_copy(update={"dataset_role": "showcase"})
    monkeypatch.setattr(b01_main, "get_settings", lambda: production_settings)
    monkeypatch.setattr(fixed_demo, "get_settings", lambda: showcase_worker_settings)
    monkeypatch.setattr(worker, "SessionLocal", ShowcaseSessionLocal)
    password = os.environ["SHOWCASE_TEST_PASSWORD"]
    with ShowcaseSessionLocal() as db:
        prior_dead_count = db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == "DEAD")
        )
        prior_qr = set(
            db.scalars(
                select(TransportPlan.published_qr_token).where(
                    TransportPlan.published_qr_token.is_not(None)
                )
            )
        )

    with TestClient(b01_app, base_url="https://testserver") as client:
        login = client.post(
            "/api/v1/web/auth/login",
            json={"username": "showcase-admin", "password": password},
        )
        assert login.status_code == 200
        access_token = login.json()["access_token"]
        revision = login.json()["case_revision"]
        csrf = client.cookies.get("blacksoil_csrf")
        reset = client.post(
            "/api/v1/web/demo-case/reset",
            json={"expected_case_revision": revision},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Idempotency-Key": f"pg-showcase-reset-{revision}",
            },
        )
        assert reset.status_code == 202
        during = client.get("/api/v1/public/dashboard/snapshot?period=30d")
        assert during.status_code == 200
        assert len(during.json()["charts"]["daily_operations"]) == 30
        assert _process_showcase_events() > 0
        stale_access = client.get(
            "/api/v1/web/demo-case/status",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert stale_access.status_code == 401
        stale_refresh = client.post(
            "/api/v1/web/auth/refresh",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert stale_refresh.status_code == 401

    with ShowcaseSessionLocal() as db:
        installation = db.scalar(select(DemoCaseInstallation))
        current_qr = set(
            db.scalars(
                select(TransportPlan.published_qr_token).where(
                    TransportPlan.published_qr_token.is_not(None)
                )
            )
        )
        assert installation.state == "READY"
        assert installation.case_revision == revision + 1
        assert prior_qr.isdisjoint(current_qr)
        current_dead_count = db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == "DEAD")
        )
        assert current_dead_count == prior_dead_count
