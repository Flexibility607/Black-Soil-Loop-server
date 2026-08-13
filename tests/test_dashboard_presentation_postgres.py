from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import func, select, update

from app.shared.database import SessionLocal, engine
from app.shared.errors import BusinessError
from app.shared.models import DashboardMapPoint, OutboxEvent, utcnow
from app.shared.optimistic import atomic_versioned_update
from app.worker import process_pending_once

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="需要 PostgreSQL 验证地图目录并发和安全投影事件",
)


def _new_map_point() -> tuple[str, int]:
    code = f"PG-CAS-{uuid4().hex[:16]}"
    now = utcnow()
    with SessionLocal() as db:
        point = DashboardMapPoint(
            point_code=code,
            display_name="并发目录点",
            point_type="TRADITIONAL_STORE",
            brand_name="测试品牌",
            address="长春市测试地址",
            longitude=125.35,
            latitude=43.85,
            coordinate_crs="EPSG:4326",
            coordinate_accuracy="DEMO_APPROXIMATE",
            source_crs="WGS84",
            featured=False,
            display_order=9999,
            source_type="DEMO_SIMULATION",
            source_name="PostgreSQL 并发测试",
            source_accessed_at=now,
            verified_at=now,
            catalog_version="postgres-test",
            catalog_sha256="f" * 64,
            enabled=False,
        )
        db.add(point)
        db.flush()
        result = (point.id, point.object_version)
        db.commit()
        return result


def test_postgres_map_catalog_cas_allows_exactly_one_update(seeded_database) -> None:
    point_id, original_version = _new_map_point()
    barrier = Barrier(2, timeout=15)

    def rename(display_name: str) -> str:
        with SessionLocal() as db:
            point = db.get(DashboardMapPoint, point_id)
            assert point is not None
            assert point.object_version == original_version
            barrier.wait()
            try:
                atomic_versioned_update(
                    db,
                    DashboardMapPoint,
                    point_id,
                    original_version,
                    {"display_name": display_name},
                )
                db.commit()
                return "SUCCESS"
            except BusinessError as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(rename, ("并发目录点甲", "并发目录点乙")))

    assert sorted(outcomes) == ["SUCCESS", "VERSION_CONFLICT"]
    with SessionLocal() as db:
        point = db.get(DashboardMapPoint, point_id)
        assert point is not None
        assert point.object_version == original_version + 1
        assert point.display_name in {"并发目录点甲", "并发目录点乙"}


def test_postgres_business_event_publishes_one_safe_snapshot_event_within_five_seconds(
    seeded_database,
) -> None:
    source_id = str(uuid4())
    refresh_id = str(uuid5(UUID(source_id), "dashboard.projection.refresh_requested"))
    notification_id = str(uuid5(UUID(refresh_id), "dashboard.snapshot.updated"))
    with SessionLocal() as db:
        db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.status.in_(["PENDING", "PROCESSING"]))
            .values(status="PUBLISHED", claimed_by=None, claimed_at=None)
        )
        db.add(
            OutboxEvent(
                event_id=source_id,
                topic="test.dashboard.presentation.changed",
                object_type="test_dashboard_presentation",
                object_id=str(uuid4()),
                object_version=1,
                payload={"private_object_id": str(uuid4())},
                status="PENDING",
            )
        )
        db.commit()

    started = monotonic()
    assert process_pending_once(limit=10) == 1
    assert process_pending_once(limit=10) == 1
    elapsed = monotonic() - started

    with SessionLocal() as db:
        source = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == source_id))
        refresh = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == refresh_id))
        notification = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == notification_id))
        assert source is not None and source.status == "PUBLISHED"
        assert refresh is not None and refresh.status == "PUBLISHED"
        assert notification is not None and notification.status == "PUBLISHED"
        assert notification.topic == "dashboard.snapshot.updated"
        assert notification.payload == {
            "targets": ["public-dashboard", "e01-overview"],
            "kinds": ["operations", "map", "algorithm_showcase"],
            "periods": ["7d", "30d", "month"],
        }
        assert "object_id" not in notification.payload
        assert "object_version" not in notification.payload
        assert "payload" not in notification.payload
        assert db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_id == refresh_id)
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_id == notification_id)
        ) == 1
    assert elapsed < 5
