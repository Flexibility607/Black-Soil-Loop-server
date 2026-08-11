from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.shared.dictionaries import (
    ALERT_TYPE_LABELS,
    RECEIPT_STATUS_LABELS,
    TRANSPORT_ACTION_LABELS,
    TRANSPORT_STATUS_LABELS,
    enum_label,
)
from app.shared.errors import BusinessError, version_conflict
from app.shared.models import (
    Alert,
    AuditLog,
    IdempotencyRecord,
    InventoryBalance,
    InventoryMovement,
    LocationPoint,
    OutboxEvent,
    Receipt,
    StockoutRequest,
    StoreDailyReport,
    TaskEvent,
    TaskException,
    TaskStop,
    TelemetryIssue,
    TelemetryPoint,
    TransportTask,
    User,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update

ACTION_TRANSITIONS = {
    ("PUBLISHED", "ACCEPT"): "DRIVER_ACCEPTED",
    ("DRIVER_ACCEPTED", "PICKUP"): "PICKED_UP",
    ("PICKED_UP", "START_TRANSIT"): "IN_TRANSIT",
}

TEMPERATURE_LIMITS = {
    "AMBIENT": {"temperature": (0.0, 25.0), "humidity": (20.0, 80.0)},
    "CHILLED": {"temperature": (0.0, 8.0), "humidity": (30.0, 85.0)},
    "FROZEN": {"temperature": (-25.0, -12.0), "humidity": (20.0, 90.0)},
}


def record_telemetry_issue(
    db: Session,
    *,
    task_id: str,
    store_id: str | None,
    source_type: str,
    issue_type: str,
    idempotency_key: str,
    message: str,
    trace_id: str,
    severity: str = "WARNING",
    task_status: str | None = None,
    expected_vehicle_id: str | None = None,
    actual_vehicle_id: str | None = None,
    expected_driver_id: str | None = None,
    actual_driver_id: str | None = None,
    details: dict[str, Any] | None = None,
    actor_user_id: str | None = None,
) -> TelemetryIssue:
    source_key = f"{source_type}:{issue_type}:{stable_hash({'task_id': task_id, 'key': idempotency_key})}"
    existing = db.scalar(select(TelemetryIssue).where(TelemetryIssue.source_key == source_key))
    if existing is not None:
        return existing
    issue = TelemetryIssue(
        source_key=source_key,
        task_id=task_id,
        store_id=store_id,
        source_type=source_type,
        issue_type=issue_type,
        severity=severity,
        task_status=task_status,
        expected_vehicle_id=expected_vehicle_id,
        actual_vehicle_id=actual_vehicle_id,
        expected_driver_id=expected_driver_id,
        actual_driver_id=actual_driver_id,
        message=message,
        details=details or {},
    )
    try:
        with db.begin_nested():
            db.add(issue)
            db.flush()
    except IntegrityError as exc:
        db.expire_all()
        existing = db.scalar(select(TelemetryIssue).where(TelemetryIssue.source_key == source_key))
        if existing is None:
            raise RuntimeError("遥测一致性问题并发记录失败") from exc
        return existing
    event_payload = {
        "issue_id": issue.id,
        "task_id": task_id,
        "source_type": source_type,
        "issue_type": issue_type,
        "severity": severity,
        "task_status": task_status,
        "message": message,
    }
    add_outbox(db, "telemetry.issue.recorded", "telemetry_issue", issue.id, 1, event_payload)
    add_audit(
        db,
        trace_id,
        actor_user_id,
        "RECORD_TELEMETRY_ISSUE",
        "telemetry_issue",
        issue.id,
        None,
        event_payload,
    )
    return issue


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_idempotent(db: Session, scope: str, key: str, payload: Any) -> dict[str, Any] | None:
    request_hash = stable_hash(payload)
    query = select(IdempotencyRecord).where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)
    record = db.scalar(query)
    if record is None:
        for attempt in range(3):
            candidate = IdempotencyRecord(
                scope=scope,
                key=key,
                request_hash=request_hash,
                state="PROCESSING",
                status_code=0,
                response_body={},
            )
            try:
                with db.begin_nested():
                    db.add(candidate)
                    db.flush()
                return None
            except IntegrityError:
                db.expire_all()
                record = db.scalar(query.execution_options(populate_existing=True))
                if record is None:
                    raise
                break
            except OperationalError:
                if db.bind is None or db.bind.dialect.name != "sqlite" or attempt == 2:
                    raise
                db.expire_all()
                time.sleep(0.02 * (attempt + 1))
    if record.request_hash != request_hash:
        raise BusinessError(
            "IDEMPOTENCY_CONFLICT",
            "同一个幂等键不能用于不同请求",
            status_code=409,
            details={"scope": scope},
        )
    if record.state == "PROCESSING":
        raise BusinessError(
            "IDEMPOTENCY_IN_PROGRESS",
            "相同幂等请求正在处理中，请稍后重试",
            status_code=409,
            details={"scope": scope},
        )
    return record.response_body


def save_idempotent(
    db: Session,
    scope: str,
    key: str,
    payload: Any,
    response_body: dict[str, Any],
    status_code: int = 200,
) -> None:
    request_hash = stable_hash(payload)
    record = db.scalar(
        select(IdempotencyRecord).where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)
    )
    if record is None or record.request_hash != request_hash or record.state != "PROCESSING":
        raise RuntimeError("幂等记录未在当前事务中取得或状态无效")
    record.state = "COMPLETED"
    record.status_code = status_code
    record.response_body = response_body
    record.completed_at = utcnow()


def add_outbox(
    db: Session,
    topic: str,
    object_type: str,
    object_id: str,
    object_version: int,
    payload: dict[str, Any],
) -> OutboxEvent:
    event = OutboxEvent(
        topic=topic,
        object_type=object_type,
        object_id=object_id,
        object_version=object_version,
        payload=payload,
    )
    db.add(event)
    return event


def add_audit(
    db: Session,
    trace_id: str,
    actor_user_id: str | None,
    action: str,
    object_type: str,
    object_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    db.add(
        AuditLog(
            trace_id=trace_id,
            actor_user_id=actor_user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            before_snapshot=before,
            after_snapshot=after,
        )
    )


def ensure_task_scope(user: User, task: TransportTask) -> None:
    if user.role == "driver" and user.driver_id != task.driver_id:
        raise BusinessError("FORBIDDEN", "只能操作分配给本人的任务", status_code=403)
    if user.role in {"store_manager", "third_space_manager"} and user.store_id != task.store_id:
        raise BusinessError("FORBIDDEN", "只能操作本门店任务", status_code=403)


def create_task_qr(task_no: str) -> tuple[str, str, datetime]:
    raw = f"BSL:{task_no}:{secrets.token_urlsafe(18)}"
    token_hash_value = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires_at = utcnow() + timedelta(days=2)
    return raw, token_hash_value, expires_at


def verify_task_qr(task: TransportTask, raw: str | None) -> None:
    if not raw or not task.qr_token_hash or not task.qr_expires_at:
        raise BusinessError("QR_REQUIRED", "需要有效的货单二维码", status_code=400)
    expires_at = task.qr_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < utcnow():
        raise BusinessError("QR_EXPIRED", "二维码已过期", status_code=409)
    if not secrets.compare_digest(task.qr_token_hash, hashlib.sha256(raw.encode("utf-8")).hexdigest()):
        raise BusinessError("QR_INVALID", "二维码与当前任务不匹配", status_code=409)


def transition_task(
    db: Session,
    *,
    task: TransportTask,
    user: User,
    action: str,
    expected_version: int,
    idempotency_key: str,
    trace_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    scope = f"task-action:{task.id}"
    cached = load_idempotent(db, scope, idempotency_key, payload)
    if cached is not None:
        return cached
    ensure_task_scope(user, task)
    if task.object_version != expected_version:
        raise version_conflict(task.object_version, expected_version)

    before = {"status": task.status, "object_version": task.object_version}
    from_status = task.status
    stop_id = payload.get("stop_id")

    if action == "DELIVER":
        if task.status not in {"IN_TRANSIT", "DELIVERED"}:
            raise BusinessError("INVALID_TRANSITION", "当前状态不能执行送达", status_code=409)
        stops = list(db.scalars(select(TaskStop).where(TaskStop.task_id == task.id).order_by(TaskStop.sequence_no)))
        if not stops:
            raise BusinessError("TASK_STOPS_MISSING", "任务缺少配送站点", status_code=409)
        target = (
            next((stop for stop in stops if stop.id == stop_id), None)
            if stop_id
            else next(
                (stop for stop in stops if stop.status == "PLANNED"),
                None,
            )
        )
        if target is None:
            raise BusinessError("STOP_NOT_FOUND", "没有可送达的站点", status_code=409)
        if target.status != "PLANNED":
            raise BusinessError("STOP_ALREADY_DELIVERED", "该站点已经送达，不能重复操作", status_code=409)
        verify_task_qr(task, payload.get("qr_token"))
        atomic_versioned_update(
            db,
            TaskStop,
            target.id,
            target.object_version,
            {"status": "DELIVERED"},
            conditions=(TaskStop.status == "PLANNED",),
        )
        remaining_planned = sum(stop.status == "PLANNED" for stop in stops) - 1
        to_status = "DELIVERED" if remaining_planned == 0 else "IN_TRANSIT"
    else:
        to_status = ACTION_TRANSITIONS.get((task.status, action), "")
        if not to_status:
            raise BusinessError(
                "INVALID_TRANSITION",
                "不允许从当前状态执行该操作",
                status_code=409,
                details={"current_status": task.status, "action": action},
            )
        if action == "PICKUP":
            verify_task_qr(task, payload.get("qr_token"))

    next_task_version = atomic_versioned_update(
        db,
        TransportTask,
        task.id,
        expected_version,
        {"status": to_status},
        conditions=(TransportTask.status == from_status,),
    )
    event = TaskEvent(
        task_id=task.id,
        from_status=from_status,
        to_status=to_status,
        action=action,
        actor_user_id=user.id,
        idempotency_key=idempotency_key,
        payload={key: value for key, value in payload.items() if key != "qr_token"},
        occurred_at=payload.get("occurred_at") or utcnow(),
    )
    db.add(event)
    response = {
        "task_id": task.id,
        "task_no": task.task_no,
        "status": to_status,
        "status_label": enum_label(TRANSPORT_STATUS_LABELS, to_status),
        "action_label": enum_label(TRANSPORT_ACTION_LABELS, action),
        "object_version": next_task_version,
    }
    add_outbox(db, "transport.task.status_changed", "transport_task", task.id, next_task_version, response)
    add_audit(db, trace_id, user.id, action, "transport_task", task.id, before, response)
    save_idempotent(db, scope, idempotency_key, payload, response)
    db.commit()
    return response


def sign_receipt(
    db: Session,
    *,
    task: TransportTask,
    user: User,
    receipt_status: str,
    lines: list[dict[str, Any]],
    qr_token: str,
    expected_version: int,
    idempotency_key: str,
    trace_id: str,
) -> dict[str, Any]:
    payload = {
        "task_id": task.id,
        "receipt_status": receipt_status,
        "lines": lines,
        "object_version": expected_version,
    }
    scope = f"receipt:{task.id}:{user.store_id or task.store_id}"
    cached = load_idempotent(db, scope, idempotency_key, payload)
    if cached is not None:
        return cached
    if user.role not in {"store_manager", "third_space_manager", "park_admin"}:
        raise BusinessError("FORBIDDEN", "当前角色不能执行签收", status_code=403)
    if task.object_version != expected_version:
        raise version_conflict(task.object_version, expected_version)
    if task.status not in {"DELIVERED", "STORE_SIGNED"}:
        raise BusinessError("INVALID_TRANSITION", "任务尚未全部送达", status_code=409)
    verify_task_qr(task, qr_token)

    store_id = user.store_id or task.store_id
    stop = db.scalar(select(TaskStop).where(TaskStop.task_id == task.id, TaskStop.store_id == store_id))
    if not stop or stop.status != "DELIVERED":
        raise BusinessError("STOP_NOT_DELIVERED", "本门店站点尚未送达或已签收", status_code=409)

    expected_by_product: dict[str, float] = {}
    for item in stop.delivery_lines:
        expected_by_product[item["product_id"]] = expected_by_product.get(item["product_id"], 0) + float(
            item["expected_quantity"]
        )
    received_by_product = {line["product_id"]: float(line["received_quantity"]) for line in lines}
    declared_by_product = {line["product_id"]: float(line["expected_quantity"]) for line in lines}
    if set(received_by_product) != set(expected_by_product):
        raise BusinessError("RECEIPT_LINES_MISMATCH", "签收明细必须覆盖本站点全部商品", status_code=409)
    for product_id, expected in expected_by_product.items():
        if abs(declared_by_product[product_id] - expected) > 1e-6:
            raise BusinessError("EXPECTED_QUANTITY_MISMATCH", "应收数量与任务不一致", status_code=409)
        if received_by_product[product_id] > expected:
            raise BusinessError("RECEIVED_QUANTITY_EXCEEDED", "实收数量不能超过应收数量", status_code=409)
    if receipt_status == "FULL" and any(
        abs(received_by_product[product_id] - expected) > 1e-6 for product_id, expected in expected_by_product.items()
    ):
        raise BusinessError("FULL_RECEIPT_QUANTITY_MISMATCH", "全量签收要求实收数量等于应收数量", status_code=409)
    if receipt_status == "PARTIAL" and all(
        abs(received_by_product[product_id] - expected) <= 1e-6 for product_id, expected in expected_by_product.items()
    ):
        raise BusinessError("PARTIAL_RECEIPT_REQUIRES_DIFFERENCE", "差异签收必须存在数量差异", status_code=409)
    if receipt_status == "REJECTED" and any(received_by_product.values()):
        raise BusinessError("REJECTED_RECEIPT_REQUIRES_ZERO", "拒收时实收数量必须为零", status_code=409)

    before = {"status": task.status, "object_version": task.object_version}
    other_unsigned_stops = db.scalar(
        select(func.count(TaskStop.id)).where(
            TaskStop.task_id == task.id,
            TaskStop.id != stop.id,
            TaskStop.status != "SIGNED",
        )
    )
    next_task_status = "COMPLETED" if (other_unsigned_stops or 0) == 0 else "STORE_SIGNED"
    atomic_versioned_update(
        db,
        TaskStop,
        stop.id,
        stop.object_version,
        {"status": "SIGNED"},
        conditions=(TaskStop.status == "DELIVERED",),
    )
    next_task_version = atomic_versioned_update(
        db,
        TransportTask,
        task.id,
        expected_version,
        {"status": next_task_status},
        conditions=(TransportTask.status == before["status"],),
    )

    receipt = Receipt(
        task_id=task.id,
        store_id=store_id,
        receipt_status=receipt_status,
        details=lines,
        idempotency_key=idempotency_key,
        actor_user_id=user.id,
    )
    db.add(receipt)
    db.flush()

    for line in sorted(lines, key=lambda item: item["product_id"]):
        received = float(line["received_quantity"])
        if receipt_status == "REJECTED" or received <= 0:
            continue
        product_id = line["product_id"]
        quantity_before = 0.0
        quantity_after = received
        for attempt in range(5):
            balance = db.scalar(
                select(InventoryBalance)
                .where(
                    InventoryBalance.store_id == store_id,
                    InventoryBalance.product_id == product_id,
                )
                .execution_options(populate_existing=True)
            )
            if balance is None:
                try:
                    with db.begin_nested():
                        db.add(InventoryBalance(store_id=store_id, product_id=product_id, quantity=received))
                        db.flush()
                    quantity_before = 0.0
                    quantity_after = received
                    break
                except IntegrityError:
                    db.expire_all()
                    if attempt == 4:
                        raise
                    continue
            try:
                quantity_before = float(balance.quantity)
                quantity_after = quantity_before + received
                atomic_versioned_update(
                    db,
                    InventoryBalance,
                    balance.id,
                    balance.object_version,
                    {"quantity": InventoryBalance.quantity + received},
                )
                break
            except BusinessError as exc:
                if exc.code != "VERSION_CONFLICT" or attempt == 4:
                    raise
                db.expire_all()
        db.add(
            InventoryMovement(
                store_id=store_id,
                product_id=line["product_id"],
                movement_type="IN",
                quantity_before=quantity_before,
                quantity_delta=received,
                quantity_after=quantity_after,
                source_type="RECEIPT",
                source_id=receipt.id,
                idempotency_key=f"{idempotency_key}:{line['product_id']}",
            )
        )

    db.add(
        TaskEvent(
            task_id=task.id,
            from_status=before["status"],
            to_status="STORE_SIGNED",
            action=f"RECEIPT_{receipt_status}",
            actor_user_id=user.id,
            idempotency_key=f"{idempotency_key}:state",
            payload={"receipt_id": receipt.id, "store_id": store_id},
        )
    )
    if next_task_status == "COMPLETED":
        db.add(
            TaskEvent(
                task_id=task.id,
                from_status="STORE_SIGNED",
                to_status="COMPLETED",
                action="AUTO_COMPLETE",
                actor_user_id=user.id,
                idempotency_key=f"{idempotency_key}:complete",
                payload={"receipt_id": receipt.id},
            )
        )

    response = {
        "receipt_id": receipt.id,
        "task_id": task.id,
        "store_id": store_id,
        "receipt_status": receipt_status,
        "receipt_status_label": enum_label(RECEIPT_STATUS_LABELS, receipt_status),
        "task_status": next_task_status,
        "task_status_label": enum_label(TRANSPORT_STATUS_LABELS, next_task_status),
        "object_version": next_task_version,
    }
    add_outbox(db, "store.receipt.completed", "receipt", receipt.id, 1, response)
    add_outbox(db, "transport.task.status_changed", "transport_task", task.id, next_task_version, response)
    add_audit(db, trace_id, user.id, "SIGN_RECEIPT", "transport_task", task.id, before, response)
    save_idempotent(db, scope, idempotency_key, payload, response, 201)
    db.commit()
    return response


def create_stockout(
    db: Session,
    *,
    user: User,
    product_id: str,
    requested_quantity: float,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if not user.store_id:
        raise BusinessError("STORE_SCOPE_REQUIRED", "账号未绑定门店", status_code=403)
    payload = {"product_id": product_id, "requested_quantity": requested_quantity, "reason": reason}
    scope = f"stockout:{user.store_id}"
    cached = load_idempotent(db, scope, idempotency_key, payload)
    if cached is not None:
        return cached
    item = StockoutRequest(
        store_id=user.store_id,
        product_id=product_id,
        requested_quantity=requested_quantity,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    db.add(item)
    db.flush()
    response = {"id": item.id, "status": item.status, "object_version": item.object_version}
    add_outbox(db, "store.stockout.submitted", "stockout_request", item.id, item.object_version, response)
    save_idempotent(db, scope, idempotency_key, payload, response, 201)
    db.commit()
    return response


def create_daily_report(
    db: Session,
    *,
    user: User,
    payload: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    if not user.store_id:
        raise BusinessError("STORE_SCOPE_REQUIRED", "账号未绑定门店", status_code=403)
    scope = f"daily-report:{user.store_id}"
    cached = load_idempotent(db, scope, idempotency_key, payload)
    if cached is not None:
        return cached
    existing = db.scalar(
        select(StoreDailyReport).where(
            StoreDailyReport.store_id == user.store_id,
            StoreDailyReport.report_date == payload["report_date"],
        )
    )
    if existing:
        raise BusinessError("DAILY_REPORT_EXISTS", "该门店当天日报已提交", status_code=409)
    report = StoreDailyReport(store_id=user.store_id, idempotency_key=idempotency_key, **payload)
    db.add(report)
    db.flush()
    response = {"id": report.id, "report_date": str(report.report_date), "object_version": report.object_version}
    add_outbox(db, "store.daily_report.submitted", "store_daily_report", report.id, 1, response)
    save_idempotent(db, scope, idempotency_key, payload, response, 201)
    db.commit()
    return response


def ingest_telemetry(
    db: Session,
    task: TransportTask,
    samples: list[dict[str, Any]],
    *,
    idempotency_key: str,
    trace_id: str,
) -> dict[str, Any]:
    payload = {"task_id": task.id, "vehicle_id": task.vehicle_id, "samples": samples}
    scope = f"telemetry:{task.id}"
    cached = load_idempotent(db, scope, idempotency_key, payload)
    if cached is not None:
        return cached
    limits = TEMPERATURE_LIMITS[task.temperature_zone]
    created = 0
    anomalies: set[str] = set()
    for sample in samples:
        anomaly_codes: list[str] = []
        t_low, t_high = limits["temperature"]
        h_low, h_high = limits["humidity"]
        if not t_low <= sample["temperature_c"] <= t_high:
            anomaly_codes.append("TEMPERATURE")
        if not h_low <= sample["humidity_pct"] <= h_high:
            anomaly_codes.append("HUMIDITY")
        anomaly_code = "+".join(anomaly_codes) or None
        db.add(TelemetryPoint(task_id=task.id, vehicle_id=task.vehicle_id, anomaly_code=anomaly_code, **sample))
        created += 1
        anomalies.update(anomaly_codes)

    for alert_type in sorted(anomalies):
        existing = db.scalar(
            select(Alert).where(Alert.task_id == task.id, Alert.alert_type == alert_type, Alert.status == "OPEN")
        )
        if not existing:
            alert = Alert(task_id=task.id, alert_type=alert_type, message=f"运输任务出现{alert_type}异常")
            db.add(alert)
            db.flush()
            add_outbox(db, "transport.alert.opened", "alert", alert.id, 1, {"task_id": task.id, "type": alert_type})
    if anomalies:
        record_telemetry_issue(
            db,
            task_id=task.id,
            store_id=task.store_id,
            source_type="DEVICE_TELEMETRY",
            issue_type="ANOMALOUS_TELEMETRY",
            idempotency_key=idempotency_key,
            message=f"遥测批次出现{'、'.join(sorted(anomalies))}异常",
            trace_id=trace_id,
            severity="CRITICAL",
            task_status=task.status,
            expected_vehicle_id=task.vehicle_id,
            actual_vehicle_id=task.vehicle_id,
            details={"anomaly_types": sorted(anomalies), "sample_count": created},
        )
    response = {
        "accepted": created,
        "anomaly_types": sorted(anomalies),
        "anomaly_type_labels": [enum_label(ALERT_TYPE_LABELS, value) for value in sorted(anomalies)],
    }
    add_audit(
        db,
        trace_id,
        None,
        "INGEST_TELEMETRY",
        "transport_task",
        task.id,
        None,
        response,
    )
    save_idempotent(db, scope, idempotency_key, payload, response, 202)
    db.commit()
    return response


def create_location_point(
    db: Session,
    *,
    task: TransportTask,
    user: User,
    payload: dict[str, Any],
    idempotency_key: str,
    trace_id: str,
) -> dict[str, Any]:
    scope = f"task-location:{task.id}"
    cached = load_idempotent(db, scope, idempotency_key, payload)
    if cached is not None:
        return cached
    ensure_task_scope(user, task)
    if user.role != "driver" or not user.driver_id:
        raise BusinessError("DRIVER_REQUIRED", "只有任务司机可以上报位置", status_code=403)
    point = LocationPoint(
        task_id=task.id,
        driver_id=user.driver_id,
        idempotency_key=idempotency_key,
        **payload,
    )
    db.add(point)
    db.flush()
    response = {"id": point.id, "task_id": task.id, "recorded_at": point.recorded_at.isoformat()}
    add_outbox(db, "transport.location.recorded", "location_point", point.id, 1, response)
    add_audit(
        db,
        trace_id,
        user.id,
        "RECORD_TASK_LOCATION",
        "location_point",
        point.id,
        None,
        response,
    )
    save_idempotent(db, scope, idempotency_key, payload, response, 201)
    db.commit()
    return response


def create_task_exception(
    db: Session,
    *,
    task: TransportTask,
    user: User,
    payload: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    scope = f"task-exception:{task.id}"
    cached = load_idempotent(db, scope, idempotency_key, payload)
    if cached is not None:
        return cached
    ensure_task_scope(user, task)
    item = TaskException(
        task_id=task.id,
        exception_type=payload["exception_type"],
        reason=payload["reason"],
        original_status=task.status,
        actor_user_id=user.id,
        idempotency_key=idempotency_key,
        occurred_at=payload.get("occurred_at") or utcnow(),
    )
    db.add(item)
    db.flush()
    response = {
        "id": item.id,
        "task_id": task.id,
        "exception_type": item.exception_type,
        "original_status": item.original_status,
        "occurred_at": item.occurred_at.isoformat(),
    }
    add_outbox(db, "transport.exception.reported", "task_exception", item.id, 1, response)
    save_idempotent(db, scope, idempotency_key, payload, response, 201)
    db.commit()
    return response
