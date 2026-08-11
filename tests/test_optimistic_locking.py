from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.b02.main import app as b02_app
from app.shared.database import SessionLocal, engine
from app.shared.errors import BusinessError
from app.shared.models import (
    AuditLog,
    IdempotencyRecord,
    OutboxEvent,
    Store,
    TaskEvent,
    TransportTask,
    User,
    Vehicle,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update
from app.worker import _claim_event
from tests.conftest import login


def _create_published_task() -> tuple[str, int]:
    with SessionLocal() as db:
        driver_user = db.scalar(select(User).where(User.username == "driver_demo"))
        assert driver_user is not None
        assert driver_user.driver_id is not None
        vehicle = db.scalar(select(Vehicle).where(Vehicle.driver_id == driver_user.driver_id).order_by(Vehicle.id))
        store = db.scalar(select(Store).where(Store.enabled.is_(True)).order_by(Store.id))
        assert vehicle is not None
        assert store is not None

        task = TransportTask(
            task_no=f"CAS-{uuid4().hex[:24]}",
            source_order_ids=[],
            vehicle_id=vehicle.id,
            driver_id=driver_user.driver_id,
            store_id=store.id,
            status="PUBLISHED",
            temperature_zone=vehicle.temperature_zone,
            total_weight_kg=100.0,
            total_volume_m3=1.0,
            origin_latitude=store.latitude,
            origin_longitude=store.longitude,
            destination_latitude=store.latitude,
            destination_longitude=store.longitude,
            planned_departure_at=utcnow(),
        )
        db.add(task)
        db.flush()
        task_id = task.id
        object_version = task.object_version
        db.commit()
        return task_id, object_version


def test_atomic_versioned_update_rejects_stale_session(seeded_database) -> None:
    task_id, original_version = _create_published_task()

    with SessionLocal() as winning_db, SessionLocal() as stale_db:
        winning_task = winning_db.get(TransportTask, task_id)
        stale_task = stale_db.get(TransportTask, task_id)
        assert winning_task is not None
        assert stale_task is not None

        # Release SQLite's read transaction while retaining the stale ORM state.
        stale_db.commit()
        next_version = atomic_versioned_update(
            winning_db,
            TransportTask,
            winning_task.id,
            original_version,
            {"status": "DRIVER_ACCEPTED"},
            conditions=(TransportTask.status == "PUBLISHED",),
        )
        winning_db.commit()
        winning_db.refresh(winning_task)

        assert next_version == original_version + 1
        assert winning_task.status == "DRIVER_ACCEPTED"
        assert winning_task.object_version == original_version + 1

        with pytest.raises(BusinessError) as captured:
            atomic_versioned_update(
                stale_db,
                TransportTask,
                stale_task.id,
                original_version,
                {"status": "CANCELLED"},
                conditions=(TransportTask.status == "PUBLISHED",),
            )
        stale_db.rollback()

    error = captured.value
    assert error.code == "VERSION_CONFLICT"
    assert error.status_code == 409
    assert error.details["expected_version"] == original_version
    assert error.details["current_version"] == original_version + 1
    assert error.details["object_id"] == task_id

    with SessionLocal() as db:
        current = db.get(TransportTask, task_id)
        assert current is not None
        assert current.status == "DRIVER_ACCEPTED"
        assert current.object_version == original_version + 1


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="需要 PostgreSQL 验证真实并发写入")
def test_postgres_atomic_update_allows_exactly_one_transaction(seeded_database) -> None:
    task_id, original_version = _create_published_task()
    barrier = Barrier(2, timeout=15)

    def change_status(status: str) -> str:
        with SessionLocal() as db:
            task = db.get(TransportTask, task_id)
            assert task is not None
            assert task.object_version == original_version
            barrier.wait()
            try:
                atomic_versioned_update(
                    db,
                    TransportTask,
                    task.id,
                    original_version,
                    {"status": status},
                    conditions=(TransportTask.status == "PUBLISHED",),
                )
                db.commit()
                return "SUCCESS"
            except BusinessError as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(change_status, "DRIVER_ACCEPTED"),
            executor.submit(change_status, "CANCELLED"),
        ]
        outcomes = [future.result(timeout=30) for future in futures]

    assert sorted(outcomes) == ["SUCCESS", "VERSION_CONFLICT"]
    with SessionLocal() as db:
        task = db.get(TransportTask, task_id)
        assert task is not None
        assert task.object_version == original_version + 1
        assert task.status in {"DRIVER_ACCEPTED", "CANCELLED"}


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="需要 PostgreSQL 验证真实并发写入")
def test_postgres_outbox_claim_allows_exactly_one_worker(seeded_database) -> None:
    event_id = str(uuid4())
    with SessionLocal() as db:
        db.add(
            OutboxEvent(
                event_id=event_id,
                topic="test.concurrent-claim",
                object_type="test_object",
                object_id=str(uuid4()),
                object_version=1,
                payload={},
                status="PENDING",
            )
        )
        db.commit()

    barrier = Barrier(2, timeout=15)

    def claim(claimant: str) -> bool:
        with SessionLocal() as db:
            barrier.wait()
            won = _claim_event(db, event_id, claimant)
            if won:
                db.commit()
            else:
                db.rollback()
            return won

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ("worker-a", "worker-b")))

    assert sorted(outcomes) == [False, True]
    with SessionLocal() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == event_id))
        assert event is not None
        assert event.status == "PROCESSING"
        assert event.claimed_by in {"worker-a", "worker-b"}
        db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.event_id == event_id)
            .values(status="PUBLISHED", claimed_by=None, claimed_at=None)
        )
        db.commit()


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="需要 PostgreSQL 验证真实并发写入")
def test_concurrent_b02_accept_allows_exactly_one_request(seeded_database) -> None:
    task_id, original_version = _create_published_task()
    barrier = Barrier(2, timeout=15)
    with TestClient(b02_app) as login_client:
        access_token = login(login_client, "/api/v1/mobile", "driver_demo")["access_token"]

    def accept(idempotency_key: str) -> tuple[int, dict]:
        with TestClient(b02_app) as client:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Idempotency-Key": idempotency_key,
            }
            barrier.wait()
            response = client.post(
                f"/api/v1/mobile/tasks/{task_id}/actions",
                json={"action": "ACCEPT", "object_version": original_version},
                headers=headers,
            )
            return response.status_code, response.json()

    keys = (f"accept-a-{uuid4().hex}", f"accept-b-{uuid4().hex}")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(accept, key) for key in keys]
        results = [future.result(timeout=30) for future in futures]

    assert sorted(status for status, _ in results) == [200, 409]
    success = next(body for status, body in results if status == 200)
    conflict = next(body for status, body in results if status == 409)
    assert success["status"] == "DRIVER_ACCEPTED"
    assert success["object_version"] == original_version + 1
    assert conflict["code"] == "VERSION_CONFLICT"
    assert conflict["details"]["expected_version"] == original_version
    assert conflict["details"]["current_version"] == original_version + 1

    with SessionLocal() as db:
        task = db.get(TransportTask, task_id)
        assert task is not None
        assert task.status == "DRIVER_ACCEPTED"
        assert task.object_version == original_version + 1

        task_event_count = db.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.action == "ACCEPT")
        )
        outbox_count = db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.topic == "transport.task.status_changed",
                OutboxEvent.object_type == "transport_task",
                OutboxEvent.object_id == task_id,
            )
        )
        audit_count = db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "ACCEPT",
                AuditLog.object_type == "transport_task",
                AuditLog.object_id == task_id,
            )
        )
        idempotency_count = db.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.scope == f"task-action:{task_id}")
        )

    assert task_event_count == 1
    assert outbox_count == 1
    assert audit_count == 1
    assert idempotency_count == 1


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="需要 PostgreSQL 验证真实并发写入")
def test_concurrent_identical_idempotency_key_executes_once(seeded_database) -> None:
    task_id, original_version = _create_published_task()
    barrier = Barrier(2, timeout=15)
    with TestClient(b02_app) as login_client:
        access_token = login(login_client, "/api/v1/mobile", "driver_demo")["access_token"]
    idempotency_key = f"same-accept-{uuid4().hex}"

    def accept() -> tuple[int, dict]:
        with TestClient(b02_app) as client:
            barrier.wait()
            response = client.post(
                f"/api/v1/mobile/tasks/{task_id}/actions",
                json={"action": "ACCEPT", "object_version": original_version},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Idempotency-Key": idempotency_key,
                },
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=30) for future in (executor.submit(accept), executor.submit(accept))]

    assert [status for status, _ in results] == [200, 200]
    assert results[0][1]["status"] == results[1][1]["status"] == "DRIVER_ACCEPTED"
    assert results[0][1]["object_version"] == results[1][1]["object_version"] == original_version + 1

    with SessionLocal() as db:
        task_event_count = db.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.action == "ACCEPT")
        )
        idempotency_count = db.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(
                IdempotencyRecord.scope == f"task-action:{task_id}",
                IdempotencyRecord.key == idempotency_key,
                IdempotencyRecord.state == "COMPLETED",
            )
        )
    assert task_event_count == 1
    assert idempotency_count == 1
