from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.scheduler import run_scheduled_jobs
from app.shared.database import SessionLocal
from app.shared.models import (
    Product,
    Store,
    TransportOrder,
    Warehouse,
    WarehousePoolPlan,
    WarehouseReservation,
)
from app.worker import process_pending_once


def _drain_worker() -> None:
    for _ in range(10):
        if process_pending_once() == 0:
            return
    raise AssertionError("Worker 在限定轮次内未清空事件")


def _create_candidate_orders() -> list[str]:
    with SessionLocal() as db:
        warehouse = db.scalar(select(Warehouse).order_by(Warehouse.code))
        product = db.scalar(
            select(Product).where(Product.temperature_zone == warehouse.temperature_zone).order_by(Product.code)
        )
        stores = list(select_result for select_result in db.scalars(select(Store).order_by(Store.code).limit(2)))
        departure = datetime.now(UTC) + timedelta(days=2)
        order_ids: list[str] = []
        for index, store in enumerate(stores, 1):
            order_id = str(uuid4())
            order_ids.append(order_id)
            db.add(
                TransportOrder(
                    id=order_id,
                    order_no=f"WHT-{uuid4().hex[:10].upper()}",
                    scenario_code=f"WAREHOUSE_TEST_{uuid4().hex[:6]}",
                    enterprise_id=store.enterprise_id,
                    product_id=product.id,
                    store_id=store.id,
                    origin_latitude=warehouse.latitude + index * 0.002,
                    origin_longitude=warehouse.longitude + index * 0.002,
                    destination_latitude=store.latitude,
                    destination_longitude=store.longitude,
                    departure_at=departure,
                    warehouse_inbound_start=departure - timedelta(hours=6),
                    warehouse_inbound_end=departure - timedelta(hours=4),
                    warehouse_outbound_start=departure - timedelta(hours=2),
                    warehouse_outbound_end=departure,
                    quantity=20,
                    unit=product.unit,
                    weight_kg=150,
                    volume_m3=1.25,
                    temperature_zone=product.temperature_zone,
                )
            )
        db.commit()
        return order_ids


def _preview_and_confirm(b01_client, admin_headers: dict[str, str], key: str) -> dict:
    order_ids = _create_candidate_orders()
    preview = b01_client.post(
        "/api/v1/web/algorithms/warehouse-pool/preview",
        json={"order_ids": order_ids},
        headers=admin_headers,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    candidate = body["candidates"][0]
    assert candidate["inbound_window"]["start"] < candidate["inbound_window"]["end"]
    assert candidate["outbound_window"]["start"] < candidate["outbound_window"]["end"]
    warehouse = next(item for item in candidate["candidate_warehouses"] if item["eligible"])
    confirmed = b01_client.post(
        f"/api/v1/web/algorithms/warehouse-pool/runs/{body['match_run_id']}/confirm",
        json={
            "match_run_id": body["match_run_id"],
            "candidate_index": 0,
            "warehouse_id": warehouse["warehouse_id"],
            "warehouse_object_version": warehouse["warehouse_object_version"],
        },
        headers={**admin_headers, "Idempotency-Key": key},
    )
    assert confirmed.status_code == 202, confirmed.text
    return confirmed.json()["plan"]


def _plan_detail(b01_client, admin_headers: dict[str, str], plan_id: str) -> dict:
    response = b01_client.get(f"/api/v1/web/warehouse-pool/plans/{plan_id}", headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()["plan"]


def test_warehouse_reserve_occupy_and_release(b01_client, admin_headers):
    plan = _preview_and_confirm(b01_client, admin_headers, f"confirm-{uuid4()}")
    original_used = plan["warehouse"]["used_m3"]
    original_reserved = plan["warehouse"]["reserved_m3"]
    _drain_worker()

    reserved = _plan_detail(b01_client, admin_headers, plan["id"])
    assert reserved["status"] == "RESERVED"
    assert reserved["reservation"]["status"] == "RESERVED"
    assert reserved["warehouse"]["reserved_m3"] == original_reserved + reserved["required_volume_m3"]

    occupied_request = b01_client.post(
        f"/api/v1/web/warehouse-pool/plans/{plan['id']}/occupy",
        json={"object_version": reserved["object_version"]},
        headers={**admin_headers, "Idempotency-Key": f"occupy-{uuid4()}"},
    )
    assert occupied_request.status_code == 202, occupied_request.text
    _drain_worker()
    occupied = _plan_detail(b01_client, admin_headers, plan["id"])
    assert occupied["status"] == "OCCUPIED"
    assert occupied["reservation"]["status"] == "OCCUPIED"
    assert occupied["warehouse"]["reserved_m3"] == original_reserved
    assert occupied["warehouse"]["used_m3"] == original_used + occupied["required_volume_m3"]

    released_request = b01_client.post(
        f"/api/v1/web/warehouse-pool/plans/{plan['id']}/release",
        json={"object_version": occupied["object_version"]},
        headers={**admin_headers, "Idempotency-Key": f"release-{uuid4()}"},
    )
    assert released_request.status_code == 202, released_request.text
    _drain_worker()
    released = _plan_detail(b01_client, admin_headers, plan["id"])
    assert released["status"] == "RELEASED"
    assert released["reservation"]["status"] == "RELEASED"
    assert released["warehouse"]["reserved_m3"] == original_reserved
    assert released["warehouse"]["used_m3"] == original_used


def test_warehouse_cancel_and_scheduler_expiry_release_capacity(b01_client, admin_headers):
    cancel_plan = _preview_and_confirm(b01_client, admin_headers, f"confirm-cancel-{uuid4()}")
    _drain_worker()
    reserved = _plan_detail(b01_client, admin_headers, cancel_plan["id"])
    cancel = b01_client.post(
        f"/api/v1/web/warehouse-pool/plans/{cancel_plan['id']}/cancel",
        json={"object_version": reserved["object_version"], "reason": "测试取消预占"},
        headers={**admin_headers, "Idempotency-Key": f"cancel-{uuid4()}"},
    )
    assert cancel.status_code == 202, cancel.text
    _drain_worker()
    cancelled = _plan_detail(b01_client, admin_headers, cancel_plan["id"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["reservation"]["status"] == "CANCELLED"

    expiring_plan = _preview_and_confirm(b01_client, admin_headers, f"confirm-expire-{uuid4()}")
    _drain_worker()
    reserved = _plan_detail(b01_client, admin_headers, expiring_plan["id"])
    with SessionLocal() as db:
        stored_plan = db.get(WarehousePoolPlan, expiring_plan["id"])
        reservation = db.scalar(
            select(WarehouseReservation).where(WarehouseReservation.warehouse_pool_plan_id == stored_plan.id)
        )
        expired_at = datetime.now(UTC) - timedelta(minutes=1)
        stored_plan.reservation_expires_at = expired_at
        reservation.expires_at = expired_at
        db.commit()
    result = run_scheduled_jobs()
    assert result["expired_warehouse_reservations"] >= 1
    expired = _plan_detail(b01_client, admin_headers, expiring_plan["id"])
    assert expired["status"] == "EXPIRED"
    assert expired["reservation"]["status"] == "EXPIRED"
    assert expired["warehouse"]["reserved_m3"] == reserved["warehouse"]["reserved_m3"] - reserved[
        "required_volume_m3"
    ]
