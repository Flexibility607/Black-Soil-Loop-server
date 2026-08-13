from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.domain.dashboard import refresh_all_dashboard_projections
from app.domain.operations import project_operational_event
from app.domain.services import add_audit, add_outbox, create_task_qr
from app.shared.database import SessionLocal
from app.shared.errors import BusinessError
from app.shared.models import (
    DeadLetter,
    OutboxEvent,
    TaskEvent,
    TaskStop,
    TransportOrder,
    TransportPlan,
    TransportTask,
    Warehouse,
    WarehousePoolPlan,
    WarehouseReservation,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update
from app.shared.security import as_utc

WORKER_ID = f"worker-{uuid4().hex[:12]}"
CLAIM_TIMEOUT = timedelta(seconds=60)
WAREHOUSE_CAPACITY_LIMIT = 0.9
WAREHOUSE_RESERVATION_TTL = timedelta(minutes=30)


def _create_execution_task(db: Session, event: OutboxEvent) -> None:
    plan = db.scalar(
        select(TransportPlan).where(TransportPlan.id == event.object_id).execution_options(populate_existing=True)
    )
    if not plan or plan.task_id:
        return
    if plan.status != "CONFIRMED" or plan.object_version != event.object_version:
        return

    candidate = event.payload["candidate"]
    task = TransportTask(
        task_no=plan.plan_no.replace("PLAN-", "BSL-", 1),
        source_order_ids=candidate["order_ids"],
        vehicle_id=candidate["vehicle_id"],
        driver_id=candidate["driver_id"],
        store_id=candidate["stops"][0]["store_id"],
        status="CONFIRMED",
        temperature_zone=candidate["temperature_zone"],
        total_weight_kg=candidate["total_weight_kg"],
        total_volume_m3=candidate["total_volume_m3"],
        origin_latitude=candidate["origin"]["latitude"],
        origin_longitude=candidate["origin"]["longitude"],
        destination_latitude=candidate["destination"]["latitude"],
        destination_longitude=candidate["destination"]["longitude"],
        planned_departure_at=datetime.fromisoformat(candidate["planned_departure_at"]),
    )
    db.add(task)
    db.flush()
    for sequence, stop in enumerate(candidate["stops"], 1):
        db.add(TaskStop(task_id=task.id, sequence_no=sequence, **stop))

    next_plan_version = atomic_versioned_update(
        db,
        TransportPlan,
        plan.id,
        event.object_version,
        {"task_id": task.id, "status": "READY"},
        conditions=(TransportPlan.status == "CONFIRMED", TransportPlan.task_id.is_(None)),
    )
    add_outbox(
        db,
        "transport.task.confirmed",
        "transport_task",
        task.id,
        task.object_version,
        {
            "plan_id": plan.id,
            "plan_object_version": next_plan_version,
            "task_id": task.id,
            "task_no": task.task_no,
            "status": task.status,
        },
    )
    add_audit(
        db,
        event.event_id,
        event.payload.get("actor_user_id"),
        "CREATE_EXECUTION_TASK",
        "transport_plan",
        plan.id,
        {"status": "CONFIRMED", "object_version": event.object_version},
        {"status": "READY", "object_version": next_plan_version, "task_id": task.id},
    )


def _publish_execution_task(db: Session, event: OutboxEvent) -> None:
    plan = db.scalar(
        select(TransportPlan).where(TransportPlan.id == event.object_id).execution_options(populate_existing=True)
    )
    if not plan or plan.status == "PUBLISHED":
        return
    if plan.status != "PUBLISH_REQUESTED" or plan.object_version != event.object_version:
        return
    task = db.get(TransportTask, plan.task_id)
    if not task or task.status != "CONFIRMED":
        raise RuntimeError("执行任务未就绪或状态错误")

    raw_qr, qr_hash, qr_expires_at = create_task_qr(task.task_no)
    next_task_version = atomic_versioned_update(
        db,
        TransportTask,
        task.id,
        task.object_version,
        {"status": "PUBLISHED", "qr_token_hash": qr_hash, "qr_expires_at": qr_expires_at},
        conditions=(TransportTask.status == "CONFIRMED",),
    )
    next_plan_version = atomic_versioned_update(
        db,
        TransportPlan,
        plan.id,
        event.object_version,
        {"status": "PUBLISHED", "published_qr_token": raw_qr, "qr_expires_at": qr_expires_at},
        conditions=(TransportPlan.status == "PUBLISH_REQUESTED", TransportPlan.task_id == task.id),
    )
    db.add(
        TaskEvent(
            task_id=task.id,
            from_status="CONFIRMED",
            to_status="PUBLISHED",
            action="PUBLISH",
            actor_user_id=event.payload["actor_user_id"],
            idempotency_key=event.payload["idempotency_key"],
            payload={"plan_id": plan.id},
        )
    )
    add_outbox(
        db,
        "transport.task.published",
        "transport_task",
        task.id,
        next_task_version,
        {
            "plan_id": plan.id,
            "plan_object_version": next_plan_version,
            "task_id": task.id,
            "task_no": task.task_no,
            "status": "PUBLISHED",
        },
    )
    add_audit(
        db,
        event.event_id,
        event.payload.get("actor_user_id"),
        "PUBLISH_EXECUTION_TASK",
        "transport_task",
        task.id,
        {"status": "CONFIRMED", "object_version": task.object_version},
        {"status": "PUBLISHED", "object_version": next_task_version},
    )


def _candidate_orders(db: Session, plan: TransportPlan, task: TransportTask | None) -> list[TransportOrder]:
    order_ids = task.source_order_ids if task else plan.candidate_snapshot.get("order_ids", [])
    unique_ids = sorted(set(order_ids))
    orders = list(
        db.scalars(select(TransportOrder).where(TransportOrder.id.in_(unique_ids)).order_by(TransportOrder.id))
    )
    if len(unique_ids) != len(order_ids) or len(orders) != len(unique_ids):
        raise RuntimeError("取消计划包含重复或缺失的运输订单")
    return orders


def _cancel_execution_task(db: Session, event: OutboxEvent) -> None:
    plan = db.scalar(
        select(TransportPlan).where(TransportPlan.id == event.object_id).execution_options(populate_existing=True)
    )
    if not plan or plan.status == "CANCELLED":
        return
    if plan.status != "CANCELLATION_REQUESTED" or plan.object_version != event.object_version:
        return
    task = db.get(TransportTask, plan.task_id) if plan.task_id else None
    orders = _candidate_orders(db, plan, task)

    if task is None:
        if any(order.status not in {"CONFIRMED", "CANCELLED"} for order in orders):
            raise RuntimeError("运输订单状态与待取消计划不一致")
        for order in orders:
            if order.status == "CANCELLED":
                continue
            atomic_versioned_update(
                db,
                TransportOrder,
                order.id,
                order.object_version,
                {"status": "CANCELLED"},
                conditions=(TransportOrder.status == "CONFIRMED",),
            )
        next_plan_version = atomic_versioned_update(
            db,
            TransportPlan,
            plan.id,
            event.object_version,
            {"status": "CANCELLED", "published_qr_token": None},
            conditions=(TransportPlan.status == "CANCELLATION_REQUESTED", TransportPlan.task_id.is_(None)),
        )
        add_outbox(
            db,
            "transport.plan.cancelled",
            "transport_plan",
            plan.id,
            next_plan_version,
            {"plan_id": plan.id, "task_id": None, "reason": event.payload["reason"]},
        )
        add_audit(
            db,
            event.event_id,
            event.payload.get("actor_user_id"),
            "CANCEL_PLAN_BEFORE_TASK",
            "transport_plan",
            plan.id,
            {"status": "CANCELLATION_REQUESTED", "object_version": event.object_version},
            {"status": "CANCELLED", "object_version": next_plan_version},
        )
        return

    if task.status not in {"CONFIRMED", "PUBLISHED", "DRIVER_ACCEPTED"}:
        reason = f"任务当前状态 {task.status} 已不允许取消"
        next_plan_version = atomic_versioned_update(
            db,
            TransportPlan,
            plan.id,
            event.object_version,
            {"status": "CANCELLATION_REJECTED", "last_error": reason},
            conditions=(TransportPlan.status == "CANCELLATION_REQUESTED",),
        )
        add_outbox(
            db,
            "transport.plan.cancellation_rejected",
            "transport_plan",
            plan.id,
            next_plan_version,
            {"plan_id": plan.id, "task_id": task.id, "reason": reason},
        )
        return

    previous = task.status
    next_task_version = atomic_versioned_update(
        db,
        TransportTask,
        task.id,
        task.object_version,
        {"status": "CANCELLED"},
        conditions=(TransportTask.status == previous,),
    )
    next_plan_version = atomic_versioned_update(
        db,
        TransportPlan,
        plan.id,
        event.object_version,
        {"status": "CANCELLED", "published_qr_token": None},
        conditions=(TransportPlan.status == "CANCELLATION_REQUESTED", TransportPlan.task_id == task.id),
    )
    if any(order.status not in {"CONFIRMED", "CANCELLED"} for order in orders):
        raise RuntimeError("运输订单状态与取消任务不一致")
    for order in orders:
        if order.status == "CANCELLED":
            continue
        atomic_versioned_update(
            db,
            TransportOrder,
            order.id,
            order.object_version,
            {"status": "CANCELLED"},
            conditions=(TransportOrder.status == "CONFIRMED",),
        )
    db.add(
        TaskEvent(
            task_id=task.id,
            from_status=previous,
            to_status="CANCELLED",
            action="CANCEL",
            actor_user_id=event.payload["actor_user_id"],
            idempotency_key=event.payload["idempotency_key"],
            payload={"reason": event.payload["reason"], "original_status": previous},
        )
    )
    payload = {
        "plan_id": plan.id,
        "plan_object_version": next_plan_version,
        "task_id": task.id,
        "original_status": previous,
        "reason": event.payload["reason"],
    }
    add_outbox(db, "transport.task.cancelled", "transport_task", task.id, next_task_version, payload)
    add_outbox(db, "transport.plan.cancelled", "transport_plan", plan.id, next_plan_version, payload)
    add_audit(
        db,
        event.event_id,
        event.payload.get("actor_user_id"),
        "CANCEL_EXECUTION_TASK",
        "transport_task",
        task.id,
        {"status": previous, "object_version": task.object_version},
        {"status": "CANCELLED", "object_version": next_task_version},
    )


def _warehouse_capacity_update(
    db: Session,
    warehouse_id: str,
    values: dict,
    conditions: tuple,
) -> int:
    for attempt in range(5):
        warehouse = db.scalar(
            select(Warehouse).where(Warehouse.id == warehouse_id).execution_options(populate_existing=True)
        )
        if warehouse is None:
            raise RuntimeError("拼仓计划引用的仓库不存在")
        try:
            return atomic_versioned_update(
                db,
                Warehouse,
                warehouse.id,
                warehouse.object_version,
                values,
                conditions=conditions,
            )
        except BusinessError as exc:
            if exc.code != "VERSION_CONFLICT" or attempt == 4:
                raise
            db.expire_all()
    raise RuntimeError("仓库容量并发更新重试次数已耗尽")


def _warehouse_reservation(db: Session, plan_id: str) -> WarehouseReservation | None:
    return db.scalar(
        select(WarehouseReservation)
        .where(WarehouseReservation.warehouse_pool_plan_id == plan_id)
        .execution_options(populate_existing=True)
    )


def _reserve_warehouse_capacity(db: Session, event: OutboxEvent) -> None:
    plan = db.scalar(
        select(WarehousePoolPlan)
        .where(WarehousePoolPlan.id == event.object_id)
        .execution_options(populate_existing=True)
    )
    if plan is None or plan.status in {"RESERVED", "OCCUPIED", "RELEASED", "CANCELLED", "EXPIRED", "REJECTED"}:
        return
    if plan.status != "RESERVATION_REQUESTED" or plan.object_version != event.object_version:
        return
    if _warehouse_reservation(db, plan.id) is not None:
        raise RuntimeError("拼仓计划已存在重复预占记录")

    volume = plan.required_volume_m3
    try:
        next_warehouse_version = _warehouse_capacity_update(
            db,
            plan.warehouse_id,
            {"reserved_m3": Warehouse.reserved_m3 + volume},
            (Warehouse.used_m3 + Warehouse.reserved_m3 + volume <= Warehouse.capacity_m3 * WAREHOUSE_CAPACITY_LIMIT,),
        )
    except BusinessError as exc:
        if exc.code != "STATE_GUARD_CONFLICT":
            raise
        reason = "仓库可用库容已变化，预占后将超过总库容的 90%"
        next_plan_version = atomic_versioned_update(
            db,
            WarehousePoolPlan,
            plan.id,
            event.object_version,
            {"status": "REJECTED", "last_error": reason},
            conditions=(WarehousePoolPlan.status == "RESERVATION_REQUESTED",),
        )
        add_outbox(
            db,
            "warehouse.pool.reservation_rejected",
            "warehouse_pool_plan",
            plan.id,
            next_plan_version,
            {"plan_id": plan.id, "warehouse_id": plan.warehouse_id, "reason": reason},
        )
        add_audit(
            db,
            event.event_id,
            event.payload.get("actor_user_id"),
            "REJECT_WAREHOUSE_RESERVATION",
            "warehouse_pool_plan",
            plan.id,
            {"status": "RESERVATION_REQUESTED", "object_version": event.object_version},
            {"status": "REJECTED", "object_version": next_plan_version, "reason": reason},
        )
        return

    expires_at = utcnow() + WAREHOUSE_RESERVATION_TTL
    reservation = WarehouseReservation(
        warehouse_pool_plan_id=plan.id,
        warehouse_id=plan.warehouse_id,
        reserved_volume_m3=volume,
        status="RESERVED",
        expires_at=expires_at,
    )
    db.add(reservation)
    db.flush()
    next_plan_version = atomic_versioned_update(
        db,
        WarehousePoolPlan,
        plan.id,
        event.object_version,
        {"status": "RESERVED", "reservation_expires_at": expires_at},
        conditions=(WarehousePoolPlan.status == "RESERVATION_REQUESTED",),
    )
    payload = {
        "plan_id": plan.id,
        "warehouse_id": plan.warehouse_id,
        "warehouse_object_version": next_warehouse_version,
        "reservation_id": reservation.id,
        "reserved_volume_m3": volume,
        "expires_at": expires_at.isoformat(),
        "status": "RESERVED",
    }
    add_outbox(db, "warehouse.pool.reserved", "warehouse_pool_plan", plan.id, next_plan_version, payload)
    add_audit(
        db,
        event.event_id,
        event.payload.get("actor_user_id"),
        "RESERVE_WAREHOUSE_CAPACITY",
        "warehouse_pool_plan",
        plan.id,
        {"status": "RESERVATION_REQUESTED", "object_version": event.object_version},
        {**payload, "object_version": next_plan_version},
    )


def _occupy_warehouse_capacity(db: Session, event: OutboxEvent) -> None:
    plan = db.scalar(
        select(WarehousePoolPlan)
        .where(WarehousePoolPlan.id == event.object_id)
        .execution_options(populate_existing=True)
    )
    if plan is None or plan.status == "OCCUPIED":
        return
    if plan.status != "OCCUPY_REQUESTED" or plan.object_version != event.object_version:
        return
    reservation = _warehouse_reservation(db, plan.id)
    if reservation is None or reservation.status != "RESERVED":
        raise RuntimeError("拼仓预占记录不存在或状态不允许占用")
    if as_utc(reservation.expires_at) <= utcnow():
        raise BusinessError("WAREHOUSE_RESERVATION_EXPIRED", "拼仓预占已经过期", status_code=409)

    volume = reservation.reserved_volume_m3
    next_warehouse_version = _warehouse_capacity_update(
        db,
        plan.warehouse_id,
        {
            "reserved_m3": Warehouse.reserved_m3 - volume,
            "used_m3": Warehouse.used_m3 + volume,
        },
        (Warehouse.reserved_m3 >= volume,),
    )
    occupied_at = utcnow()
    next_reservation_version = atomic_versioned_update(
        db,
        WarehouseReservation,
        reservation.id,
        reservation.object_version,
        {"status": "OCCUPIED", "occupied_at": occupied_at},
        conditions=(WarehouseReservation.status == "RESERVED", WarehouseReservation.expires_at > occupied_at),
    )
    next_plan_version = atomic_versioned_update(
        db,
        WarehousePoolPlan,
        plan.id,
        event.object_version,
        {"status": "OCCUPIED"},
        conditions=(WarehousePoolPlan.status == "OCCUPY_REQUESTED",),
    )
    payload = {
        "plan_id": plan.id,
        "warehouse_id": plan.warehouse_id,
        "warehouse_object_version": next_warehouse_version,
        "reservation_object_version": next_reservation_version,
        "status": "OCCUPIED",
        "occupied_at": occupied_at.isoformat(),
    }
    add_outbox(db, "warehouse.pool.occupied", "warehouse_pool_plan", plan.id, next_plan_version, payload)
    add_audit(
        db,
        event.event_id,
        event.payload.get("actor_user_id"),
        "OCCUPY_WAREHOUSE_CAPACITY",
        "warehouse_pool_plan",
        plan.id,
        {"status": "OCCUPY_REQUESTED", "object_version": event.object_version},
        {**payload, "object_version": next_plan_version},
    )


def _cancel_warehouse_reservation(db: Session, event: OutboxEvent) -> None:
    plan = db.scalar(
        select(WarehousePoolPlan)
        .where(WarehousePoolPlan.id == event.object_id)
        .execution_options(populate_existing=True)
    )
    if plan is None or plan.status == "CANCELLED":
        return
    if plan.status != "CANCELLATION_REQUESTED" or plan.object_version != event.object_version:
        return
    reservation = _warehouse_reservation(db, plan.id)
    next_warehouse_version: int | None = None
    next_reservation_version: int | None = None
    released_at = utcnow()
    if reservation is not None:
        if reservation.status != "RESERVED":
            raise RuntimeError("当前拼仓预占状态不允许取消")
        volume = reservation.reserved_volume_m3
        next_warehouse_version = _warehouse_capacity_update(
            db,
            plan.warehouse_id,
            {"reserved_m3": Warehouse.reserved_m3 - volume},
            (Warehouse.reserved_m3 >= volume,),
        )
        next_reservation_version = atomic_versioned_update(
            db,
            WarehouseReservation,
            reservation.id,
            reservation.object_version,
            {"status": "CANCELLED", "released_at": released_at},
            conditions=(WarehouseReservation.status == "RESERVED",),
        )
    next_plan_version = atomic_versioned_update(
        db,
        WarehousePoolPlan,
        plan.id,
        event.object_version,
        {"status": "CANCELLED"},
        conditions=(WarehousePoolPlan.status == "CANCELLATION_REQUESTED",),
    )
    payload = {
        "plan_id": plan.id,
        "warehouse_id": plan.warehouse_id,
        "warehouse_object_version": next_warehouse_version,
        "reservation_object_version": next_reservation_version,
        "status": "CANCELLED",
        "reason": plan.cancellation_reason,
    }
    add_outbox(db, "warehouse.pool.cancelled", "warehouse_pool_plan", plan.id, next_plan_version, payload)
    add_audit(
        db,
        event.event_id,
        event.payload.get("actor_user_id"),
        "CANCEL_WAREHOUSE_RESERVATION",
        "warehouse_pool_plan",
        plan.id,
        {"status": "CANCELLATION_REQUESTED", "object_version": event.object_version},
        {**payload, "object_version": next_plan_version},
    )


def _release_warehouse_capacity(db: Session, event: OutboxEvent) -> None:
    plan = db.scalar(
        select(WarehousePoolPlan)
        .where(WarehousePoolPlan.id == event.object_id)
        .execution_options(populate_existing=True)
    )
    if plan is None or plan.status == "RELEASED":
        return
    if plan.status != "RELEASE_REQUESTED" or plan.object_version != event.object_version:
        return
    reservation = _warehouse_reservation(db, plan.id)
    if reservation is None or reservation.status != "OCCUPIED":
        raise RuntimeError("当前拼仓占用状态不允许释放")
    volume = reservation.reserved_volume_m3
    next_warehouse_version = _warehouse_capacity_update(
        db,
        plan.warehouse_id,
        {"used_m3": Warehouse.used_m3 - volume},
        (Warehouse.used_m3 >= volume,),
    )
    released_at = utcnow()
    next_reservation_version = atomic_versioned_update(
        db,
        WarehouseReservation,
        reservation.id,
        reservation.object_version,
        {"status": "RELEASED", "released_at": released_at},
        conditions=(WarehouseReservation.status == "OCCUPIED",),
    )
    next_plan_version = atomic_versioned_update(
        db,
        WarehousePoolPlan,
        plan.id,
        event.object_version,
        {"status": "RELEASED"},
        conditions=(WarehousePoolPlan.status == "RELEASE_REQUESTED",),
    )
    payload = {
        "plan_id": plan.id,
        "warehouse_id": plan.warehouse_id,
        "warehouse_object_version": next_warehouse_version,
        "reservation_object_version": next_reservation_version,
        "status": "RELEASED",
        "released_at": released_at.isoformat(),
    }
    add_outbox(db, "warehouse.pool.released", "warehouse_pool_plan", plan.id, next_plan_version, payload)
    add_audit(
        db,
        event.event_id,
        event.payload.get("actor_user_id"),
        "RELEASE_WAREHOUSE_CAPACITY",
        "warehouse_pool_plan",
        plan.id,
        {"status": "RELEASE_REQUESTED", "object_version": event.object_version},
        {**payload, "object_version": next_plan_version},
    )


def _process_event(db: Session, event: OutboxEvent) -> None:
    if event.topic == "dashboard.projection.refresh_requested":
        refresh_all_dashboard_projections(db, commit=False)
        notification_id = str(uuid5(UUID(event.event_id), "dashboard.snapshot.updated"))
        existing = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == notification_id))
        if existing is None:
            db.add(
                OutboxEvent(
                    event_id=notification_id,
                    topic="dashboard.snapshot.updated",
                    object_type="dashboard_projection",
                    object_id="public-dashboard",
                    object_version=1,
                    payload={
                        "targets": ["public-dashboard", "e01-overview"],
                        "kinds": event.payload.get("kinds", ["operations"]),
                        "periods": ["7d", "30d", "month"],
                    },
                    status="PUBLISHED",
                )
            )
        return
    if event.topic == "dashboard.snapshot.updated":
        return
    if project_operational_event(db, event):
        return
    if event.topic == "transport.plan.confirmed":
        _create_execution_task(db, event)
    elif event.topic == "transport.plan.publish_requested":
        _publish_execution_task(db, event)
    elif event.topic == "transport.plan.cancellation_requested":
        _cancel_execution_task(db, event)
    elif event.topic == "warehouse.pool.reservation_requested":
        _reserve_warehouse_capacity(db, event)
    elif event.topic == "warehouse.pool.occupy_requested":
        _occupy_warehouse_capacity(db, event)
    elif event.topic == "warehouse.pool.cancel_requested":
        _cancel_warehouse_reservation(db, event)
    elif event.topic == "warehouse.pool.release_requested":
        _release_warehouse_capacity(db, event)
    elif event.topic == "transport.task.status_changed":
        task = db.get(TransportTask, event.object_id)
        plan = db.scalar(select(TransportPlan).where(TransportPlan.task_id == event.object_id))
        if (
            event.payload.get("status") == "DRIVER_ACCEPTED"
            and plan
            and plan.status == "PUBLISHED"
            and plan.published_qr_token is not None
        ):
            try:
                atomic_versioned_update(
                    db,
                    TransportPlan,
                    plan.id,
                    plan.object_version,
                    {"published_qr_token": None},
                    conditions=(TransportPlan.status == "PUBLISHED", TransportPlan.published_qr_token.is_not(None)),
                )
            except BusinessError as exc:
                if exc.code != "VERSION_CONFLICT":
                    raise
                db.expire_all()
        if event.payload.get("status") == "COMPLETED" and task:
            orders = list(
                db.scalars(
                    select(TransportOrder)
                    .where(TransportOrder.id.in_(task.source_order_ids))
                    .order_by(TransportOrder.id)
                )
            )
            if any(order.status not in {"CONFIRMED", "COMPLETED"} for order in orders):
                raise RuntimeError("运输订单状态与完成任务不一致")
            for order in orders:
                if order.status == "COMPLETED":
                    continue
                atomic_versioned_update(
                    db,
                    TransportOrder,
                    order.id,
                    order.object_version,
                    {"status": "COMPLETED"},
                    conditions=(TransportOrder.status == "CONFIRMED",),
                )


def _claim_event(db: Session, event_id: str, claimant: str) -> bool:
    now = utcnow()
    stale_before = now - CLAIM_TIMEOUT
    claimed = db.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.event_id == event_id,
            OutboxEvent.available_at <= now,
            or_(
                OutboxEvent.status == "PENDING",
                (OutboxEvent.status == "PROCESSING")
                & (OutboxEvent.claimed_at.is_(None) | (OutboxEvent.claimed_at < stale_before)),
            ),
        )
        .values(status="PROCESSING", claimed_by=claimant, claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    return claimed.rowcount == 1


def _record_failure(event_id: str, error: Exception) -> None:
    claimant = f"{WORKER_ID}-failure"
    with SessionLocal() as db:
        if not _claim_event(db, event_id, claimant):
            db.rollback()
            return
        failed = db.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.event_id == event_id, OutboxEvent.claimed_by == claimant)
            .execution_options(populate_existing=True)
        )
        if failed is None:
            db.rollback()
            return
        failed.attempts += 1
        failed.claimed_by = None
        failed.claimed_at = None
        if failed.attempts >= 5:
            failed.status = "DEAD"
            db.add(
                DeadLetter(
                    event_id=failed.event_id,
                    topic=failed.topic,
                    payload=failed.payload,
                    error_message=str(error)[:1000],
                    attempts=failed.attempts,
                )
            )
        else:
            failed.status = "PENDING"
        db.commit()


def process_pending_once(limit: int = 100) -> int:
    processed = 0
    stale_before = utcnow() - CLAIM_TIMEOUT
    with SessionLocal() as db:
        event_ids = list(
            db.scalars(
                select(OutboxEvent.event_id)
                .where(
                    OutboxEvent.available_at <= utcnow(),
                    or_(
                        OutboxEvent.status == "PENDING",
                        (OutboxEvent.status == "PROCESSING")
                        & (OutboxEvent.claimed_at.is_(None) | (OutboxEvent.claimed_at < stale_before)),
                    ),
                )
                .order_by(OutboxEvent.sequence)
                .limit(limit)
            )
        )
    for event_id in event_ids:
        with SessionLocal() as db:
            if not _claim_event(db, event_id, WORKER_ID):
                db.rollback()
                continue
            event = db.scalar(
                select(OutboxEvent)
                .where(OutboxEvent.event_id == event_id, OutboxEvent.claimed_by == WORKER_ID)
                .execution_options(populate_existing=True)
            )
            if event is None:
                db.rollback()
                continue
            try:
                _process_event(db, event)
                if event.topic not in {"dashboard.snapshot.updated", "dashboard.projection.refresh_requested"}:
                    refresh_id = str(uuid5(UUID(event.event_id), "dashboard.projection.refresh_requested"))
                    if db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == refresh_id)) is None:
                        db.add(
                            OutboxEvent(
                                event_id=refresh_id,
                                topic="dashboard.projection.refresh_requested",
                                object_type="dashboard_projection",
                                object_id="public-dashboard",
                                object_version=1,
                                payload={
                                    "source_topic": event.topic,
                                    "kinds": ["operations", "map", "algorithm_showcase"],
                                },
                            )
                        )
                published = db.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.event_id == event_id,
                        OutboxEvent.status == "PROCESSING",
                        OutboxEvent.claimed_by == WORKER_ID,
                    )
                    .values(
                        status="PUBLISHED",
                        attempts=OutboxEvent.attempts + 1,
                        claimed_by=None,
                        claimed_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if published.rowcount != 1:
                    raise RuntimeError("Outbox 事件领取状态已变化")
                db.commit()
                processed += 1
            except Exception as exc:
                db.rollback()
                _record_failure(event_id, exc)
                continue
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="处理跨 B01/B02 outbox 并刷新看板投影")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        print({"processed": process_pending_once()})
        return
    while True:
        process_pending_once()
        time.sleep(2)


if __name__ == "__main__":
    main()
