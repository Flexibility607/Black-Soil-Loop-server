from __future__ import annotations

import argparse
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.dashboard import refresh_dashboard_projection
from app.domain.services import add_outbox, create_task_qr
from app.shared.database import SessionLocal
from app.shared.models import (
    DeadLetter,
    OutboxEvent,
    TaskEvent,
    TaskStop,
    TransportOrder,
    TransportPlan,
    TransportTask,
    utcnow,
)


def _create_execution_task(db: Session, event: OutboxEvent) -> None:
    plan = db.get(TransportPlan, event.object_id)
    if not plan or plan.task_id:
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
    plan.task_id = task.id
    plan.status = "READY"
    plan.object_version += 1
    add_outbox(
        db,
        "transport.task.confirmed",
        "transport_task",
        task.id,
        task.object_version,
        {"plan_id": plan.id, "task_id": task.id, "task_no": task.task_no, "status": task.status},
    )


def _publish_execution_task(db: Session, event: OutboxEvent) -> None:
    plan = db.get(TransportPlan, event.object_id)
    if not plan or plan.status == "PUBLISHED":
        return
    task = db.get(TransportTask, plan.task_id)
    if not task or task.status != "CONFIRMED":
        raise RuntimeError("执行任务未就绪或状态错误")
    raw_qr = create_task_qr(task)
    task.status = "PUBLISHED"
    task.object_version += 1
    plan.status = "PUBLISHED"
    plan.published_qr_token = raw_qr
    plan.qr_expires_at = task.qr_expires_at
    plan.object_version += 1
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
        task.object_version,
        {"plan_id": plan.id, "task_id": task.id, "task_no": task.task_no, "status": task.status},
    )


def _cancel_execution_task(db: Session, event: OutboxEvent) -> None:
    plan = db.get(TransportPlan, event.object_id)
    if not plan or plan.status == "CANCELLED":
        return
    task = db.get(TransportTask, plan.task_id) if plan.task_id else None
    if task is None:
        plan.status = "CANCELLED"
        plan.published_qr_token = None
        plan.object_version += 1
        return
    if task.status not in {"CONFIRMED", "PUBLISHED", "DRIVER_ACCEPTED"}:
        plan.status = "CANCELLATION_REJECTED"
        plan.last_error = f"任务当前状态 {task.status} 已不允许取消"
        plan.object_version += 1
        add_outbox(
            db,
            "transport.plan.cancellation_rejected",
            "transport_plan",
            plan.id,
            plan.object_version,
            {"plan_id": plan.id, "task_id": task.id, "reason": plan.last_error},
        )
        return
    previous = task.status
    task.status = "CANCELLED"
    task.object_version += 1
    plan.status = "CANCELLED"
    plan.published_qr_token = None
    plan.object_version += 1
    for order in db.scalars(select(TransportOrder).where(TransportOrder.id.in_(task.source_order_ids))):
        order.status = "CANCELLED"
        order.object_version += 1
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
    add_outbox(
        db,
        "transport.task.cancelled",
        "transport_task",
        task.id,
        task.object_version,
        {"plan_id": plan.id, "task_id": task.id, "original_status": previous, "reason": event.payload["reason"]},
    )


def _process_event(db: Session, event: OutboxEvent) -> None:
    if event.topic == "transport.plan.confirmed":
        _create_execution_task(db, event)
    elif event.topic == "transport.plan.publish_requested":
        _publish_execution_task(db, event)
    elif event.topic == "transport.plan.cancellation_requested":
        _cancel_execution_task(db, event)
    elif event.topic == "transport.task.status_changed":
        task = db.get(TransportTask, event.object_id)
        plan = db.scalar(select(TransportPlan).where(TransportPlan.task_id == event.object_id))
        if event.payload.get("status") == "DRIVER_ACCEPTED" and plan:
            plan.published_qr_token = None
            plan.object_version += 1
        if event.payload.get("status") == "COMPLETED" and task:
            for order in db.scalars(select(TransportOrder).where(TransportOrder.id.in_(task.source_order_ids))):
                order.status = "COMPLETED"
                order.object_version += 1


def process_pending_once(limit: int = 100) -> int:
    processed = 0
    with SessionLocal() as db:
        event_ids = list(
            db.scalars(
                select(OutboxEvent.event_id)
                .where(OutboxEvent.status == "PENDING", OutboxEvent.available_at <= utcnow())
                .order_by(OutboxEvent.sequence)
                .limit(limit)
            )
        )
    for event_id in event_ids:
        with SessionLocal() as db:
            event = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == event_id).with_for_update())
            if not event or event.status != "PENDING":
                continue
            try:
                _process_event(db, event)
                event.status = "PUBLISHED"
                event.attempts += 1
                refresh_dashboard_projection(db, commit=False)
                db.commit()
                processed += 1
            except Exception as exc:
                db.rollback()
                failed = db.scalar(select(OutboxEvent).where(OutboxEvent.event_id == event_id).with_for_update())
                if failed is None:
                    continue
                failed.attempts += 1
                if failed.attempts >= 5:
                    failed.status = "DEAD"
                    db.add(
                        DeadLetter(
                            event_id=failed.event_id,
                            topic=failed.topic,
                            payload=failed.payload,
                            error_message=str(exc)[:1000],
                            attempts=failed.attempts,
                        )
                    )
                db.commit()
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
