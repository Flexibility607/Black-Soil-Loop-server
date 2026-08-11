from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.shared.dictionaries import RECEIPT_STATUS_LABELS, enum_label
from app.shared.models import (
    InventoryMovement,
    InventoryMovementProjection,
    OutboxEvent,
    Product,
    Receipt,
    ReceiptProjection,
    StockoutDemandProjection,
    StockoutRequest,
    Store,
    StoreDailyReport,
    StoreDailyReportProjection,
    TelemetryIssue,
    TelemetryIssueProjection,
    User,
)
from app.shared.periods import period_window


def project_operational_event(db: Session, event: OutboxEvent) -> bool:
    if event.topic == "store.receipt.completed":
        return _project_receipt(db, event)
    if event.topic == "store.stockout.submitted":
        return _project_stockout(db, event)
    if event.topic == "store.daily_report.submitted":
        return _project_daily_report(db, event)
    if event.topic == "telemetry.issue.recorded":
        return _project_telemetry_issue(db, event)
    return False


def _project_receipt(db: Session, event: OutboxEvent) -> bool:
    receipt = db.get(Receipt, event.object_id)
    if receipt is None:
        raise RuntimeError("签收来源记录不存在")
    existing = db.scalar(
        select(ReceiptProjection).where(ReceiptProjection.source_receipt_id == receipt.id)
    )
    created = False
    if existing is None:
        expected_total = sum(float(line["expected_quantity"]) for line in receipt.details)
        received_total = sum(float(line["received_quantity"]) for line in receipt.details)
        rejected_line_count = sum(
            1
            for line in receipt.details
            if receipt.receipt_status == "REJECTED" or float(line["received_quantity"]) <= 0
        )
        db.add(
            ReceiptProjection(
                source_receipt_id=receipt.id,
                source_event_id=event.event_id,
                source_version=event.object_version,
                task_id=receipt.task_id,
                store_id=receipt.store_id,
                receipt_status=receipt.receipt_status,
                expected_total=expected_total,
                received_total=received_total,
                difference_total=max(expected_total - received_total, 0.0),
                rejected_line_count=rejected_line_count,
                details=receipt.details,
                business_at=receipt.received_at,
            )
        )
        created = True

    movements = list(
        db.scalars(
            select(InventoryMovement)
            .where(InventoryMovement.source_type == "RECEIPT", InventoryMovement.source_id == receipt.id)
            .order_by(InventoryMovement.id)
        )
    )
    projected_movement_ids = set(
        db.scalars(
            select(InventoryMovementProjection.source_movement_id).where(
                InventoryMovementProjection.source_movement_id.in_([item.id for item in movements])
            )
        )
    ) if movements else set()
    for movement in movements:
        if movement.id in projected_movement_ids:
            continue
        db.add(
            InventoryMovementProjection(
                source_movement_id=movement.id,
                source_event_id=event.event_id,
                source_version=1,
                store_id=movement.store_id,
                product_id=movement.product_id,
                movement_type=movement.movement_type,
                quantity_before=movement.quantity_before,
                quantity_delta=movement.quantity_delta,
                quantity_after=movement.quantity_after,
                quantity_context=movement.quantity_context,
                source_type=movement.source_type,
                source_id=movement.source_id,
                business_at=movement.occurred_at,
            )
        )
        created = True
    return created


def _project_stockout(db: Session, event: OutboxEvent) -> bool:
    source = db.get(StockoutRequest, event.object_id)
    if source is None:
        raise RuntimeError("缺货需求来源记录不存在")
    projection = db.scalar(
        select(StockoutDemandProjection).where(StockoutDemandProjection.source_stockout_id == source.id)
    )
    if projection is not None and projection.source_version >= event.object_version:
        return False
    values = {
        "source_event_id": event.event_id,
        "source_version": event.object_version,
        "store_id": source.store_id,
        "product_id": source.product_id,
        "requested_quantity": source.requested_quantity,
        "reason": source.reason,
        "status": source.status,
        "business_at": source.updated_at,
    }
    if projection is None:
        db.add(StockoutDemandProjection(source_stockout_id=source.id, **values))
    else:
        for field, value in values.items():
            setattr(projection, field, value)
    return True


def _project_daily_report(db: Session, event: OutboxEvent) -> bool:
    source = db.get(StoreDailyReport, event.object_id)
    if source is None:
        raise RuntimeError("经营日报来源记录不存在")
    projection = db.scalar(
        select(StoreDailyReportProjection).where(StoreDailyReportProjection.source_report_id == source.id)
    )
    if projection is not None and projection.source_version >= event.object_version:
        return False
    values = {
        "source_event_id": event.event_id,
        "source_version": event.object_version,
        "store_id": source.store_id,
        "report_date": source.report_date,
        "sales_amount": source.sales_amount,
        "order_count": source.order_count,
        "authorized_for_dashboard": source.authorized_for_dashboard,
        "summary": source.summary,
        "business_at": source.updated_at,
    }
    if projection is None:
        db.add(StoreDailyReportProjection(source_report_id=source.id, **values))
    else:
        for field, value in values.items():
            setattr(projection, field, value)
    return True


def _project_telemetry_issue(db: Session, event: OutboxEvent) -> bool:
    source = db.get(TelemetryIssue, event.object_id)
    if source is None:
        raise RuntimeError("遥测一致性问题来源记录不存在")
    projection = db.scalar(
        select(TelemetryIssueProjection).where(TelemetryIssueProjection.source_issue_id == source.id)
    )
    if projection is not None and projection.source_version >= event.object_version:
        return False
    values = {
        "source_event_id": event.event_id,
        "source_version": event.object_version,
        "task_id": source.task_id,
        "store_id": source.store_id,
        "issue_type": source.issue_type,
        "source_type": source.source_type,
        "severity": source.severity,
        "status": source.status,
        "task_status": source.task_status,
        "expected_vehicle_id": source.expected_vehicle_id,
        "actual_vehicle_id": source.actual_vehicle_id,
        "expected_driver_id": source.expected_driver_id,
        "actual_driver_id": source.actual_driver_id,
        "message": source.message,
        "business_at": source.occurred_at,
    }
    if projection is None:
        db.add(TelemetryIssueProjection(source_issue_id=source.id, **values))
    else:
        for field, value in values.items():
            setattr(projection, field, value)
    return True


def scoped_store_ids(db: Session, user: User) -> set[str] | None:
    if user.role == "park_admin":
        return None
    if user.enterprise_id:
        return set(db.scalars(select(Store.id).where(Store.enterprise_id == user.enterprise_id)))
    if user.store_id:
        return {user.store_id}
    return set()


def operations_summary(db: Session, user: User, period: str = "30d") -> dict[str, Any]:
    store_ids = scoped_store_ids(db, user)
    window = period_window(period)

    def scoped(model: type[Any], *, filter_business_time: bool = True):
        statement = select(model)
        if filter_business_time:
            statement = statement.where(
                model.business_at >= window.start_at,
                model.business_at < window.end_at,
            )
        return statement if store_ids is None else statement.where(model.store_id.in_(store_ids))

    receipts = list(db.scalars(scoped(ReceiptProjection).order_by(ReceiptProjection.business_at)))
    movements = list(db.scalars(scoped(InventoryMovementProjection).order_by(InventoryMovementProjection.business_at)))
    stockouts = list(db.scalars(scoped(StockoutDemandProjection).order_by(StockoutDemandProjection.business_at)))
    reports = list(
        db.scalars(
            scoped(StoreDailyReportProjection, filter_business_time=False)
            .where(
                StoreDailyReportProjection.report_date >= window.start_date,
                StoreDailyReportProjection.report_date <= window.end_date,
            )
            .where(StoreDailyReportProjection.authorized_for_dashboard.is_(True))
            .order_by(StoreDailyReportProjection.report_date)
        )
    )

    product_ids = {item.product_id for item in movements} | {item.product_id for item in stockouts}
    product_map = {
        item.id: item for item in db.scalars(select(Product).where(Product.id.in_(product_ids)))
    } if product_ids else {}
    all_store_ids = {item.store_id for item in receipts} | {item.store_id for item in stockouts} | {
        item.store_id for item in reports
    }
    store_map = {
        item.id: item for item in db.scalars(select(Store).where(Store.id.in_(all_store_ids)))
    } if all_store_ids else {}

    receipt_counts: dict[str, int] = defaultdict(int)
    for item in receipts:
        receipt_counts[item.receipt_status] += 1

    stockout_by_product: dict[str, float] = defaultdict(float)
    third_space_by_store: dict[str, float] = defaultdict(float)
    for item in stockouts:
        if item.status in {"FULFILLED", "CANCELLED"}:
            continue
        stockout_by_product[item.product_id] += item.requested_quantity
        store = store_map.get(item.store_id)
        if store and store.channel == "THIRD_SPACE":
            third_space_by_store[item.store_id] += item.requested_quantity

    movement_by_product: dict[str, float] = defaultdict(float)
    for item in movements:
        movement_by_product[item.product_id] += item.quantity_delta

    cutoff_candidates = [
        *(item.business_at for item in receipts),
        *(item.business_at for item in movements),
        *(item.business_at for item in stockouts),
        *(item.business_at for item in reports),
    ]
    return {
        "period": period,
        "unit": "综合指标",
        "data_cutoff": max(cutoff_candidates) if cutoff_candidates else None,
        "receipts": {
            "unit": "单",
            "status": [
                {"code": code, "label": enum_label(RECEIPT_STATUS_LABELS, code), "count": count}
                for code, count in sorted(receipt_counts.items())
            ],
            "difference_count": sum(item.receipt_status == "PARTIAL" for item in receipts),
            "rejected_count": sum(item.receipt_status == "REJECTED" for item in receipts),
            "difference_quantity": sum(item.difference_total for item in receipts),
        },
        "stockouts": {
            "unit": "商品基础单位",
            "ranking": [
                {
                    "product_id": product_id,
                    "product_name": product_map[product_id].name if product_id in product_map else "未知商品",
                    "product_unit": product_map[product_id].unit if product_id in product_map else "未知单位",
                    "quantity": quantity,
                }
                for product_id, quantity in sorted(
                    stockout_by_product.items(), key=lambda pair: (-pair[1], pair[0])
                )
            ],
            "third_space": [
                {
                    "store_id": store_id,
                    "store_name": store_map[store_id].name if store_id in store_map else "未知第三空间",
                    "quantity": quantity,
                }
                for store_id, quantity in sorted(
                    third_space_by_store.items(), key=lambda pair: (-pair[1], pair[0])
                )
            ],
        },
        "inventory": {
            "unit": "商品基础单位",
            "changes": [
                {
                    "product_id": product_id,
                    "product_name": product_map[product_id].name if product_id in product_map else "未知商品",
                    "product_unit": product_map[product_id].unit if product_id in product_map else "未知单位",
                    "quantity_delta": quantity,
                }
                for product_id, quantity in sorted(
                    movement_by_product.items(), key=lambda pair: (-abs(pair[1]), pair[0])
                )
            ],
        },
        "daily_reports": {
            "unit": "人民币/单",
            "report_count": len(reports),
            "sales_amount": float(sum((item.sales_amount for item in reports), Decimal("0"))),
            "order_count": sum(item.order_count for item in reports),
        },
    }
