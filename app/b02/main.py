from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, File, Header, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.services import (
    add_audit,
    add_outbox,
    create_daily_report,
    create_location_point,
    create_stockout,
    create_task_exception,
    ingest_telemetry,
    load_idempotent,
    record_telemetry_issue,
    save_idempotent,
    sign_receipt,
    transition_task,
)
from app.shared.auth_api import issue_user_tokens, login_user, logout_user, refresh_user
from app.shared.config import get_settings
from app.shared.database import SessionLocal, engine, get_db, init_database
from app.shared.dependencies import get_current_user, require_roles
from app.shared.dictionaries import (
    ALERT_STATUS_LABELS,
    ALERT_TYPE_LABELS,
    ROLE_LABELS,
    TEMPERATURE_ZONE_LABELS,
    TRANSPORT_ACTION_LABELS,
    TRANSPORT_STATUS_LABELS,
    enum_label,
)
from app.shared.errors import BusinessError, version_conflict
from app.shared.http import configure_app
from app.shared.models import (
    Alert,
    Attachment,
    InventoryBalance,
    OutboxEvent,
    Receipt,
    StockoutRequest,
    StoreDailyReport,
    TaskStop,
    TelemetryIssue,
    TransportTask,
    User,
    UserSession,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update
from app.shared.responses import api_payload, page_payload
from app.shared.schemas import (
    AlertUpdate,
    DailyReportCreate,
    LocationCreate,
    LoginRequest,
    ReceiptCreate,
    RefreshRequest,
    StockoutCreate,
    TaskActionRequest,
    TaskExceptionCreate,
    TelemetryBatch,
    WechatLoginRequest,
)
from app.shared.security import as_utc, decode_token


@asynccontextmanager
async def lifespan(_: FastAPI):
    if engine.dialect.name == "sqlite":
        init_database()
    yield


app = configure_app(
    FastAPI(
        title="黑土循环 B02 移动履约服务",
        version="0.1.0",
        description="司机、店长、第三空间、签收、库存、遥测、报警与实时事件 API",
        lifespan=lifespan,
    )
)

MOBILE = "/api/v1/mobile"
WS_SESSION_CHECK_INTERVAL_SECONDS = 30
WS_HEARTBEAT_INTERVAL_SECONDS = 5
LOCATION_UPLOAD_STATUSES = frozenset({"DRIVER_ACCEPTED", "PICKED_UP", "IN_TRANSIT"})
TELEMETRY_UPLOAD_STATUSES = frozenset({"PICKED_UP", "IN_TRANSIT", "DELIVERED"})


def _record_rejected_upload(
    db: Session,
    *,
    task_id: str,
    source_type: str,
    issue_type: str,
    idempotency_key: str,
    message: str,
    trace_id: str,
    task: TransportTask | None = None,
    actual_vehicle_id: str | None = None,
    actual_driver_id: str | None = None,
    actor_user_id: str | None = None,
) -> None:
    record_telemetry_issue(
        db,
        task_id=task_id,
        store_id=task.store_id if task else None,
        source_type=source_type,
        issue_type=issue_type,
        idempotency_key=idempotency_key,
        message=message,
        trace_id=trace_id,
        severity="WARNING",
        task_status=task.status if task else None,
        expected_vehicle_id=task.vehicle_id if task else None,
        actual_vehicle_id=actual_vehicle_id,
        expected_driver_id=task.driver_id if task else None,
        actual_driver_id=actual_driver_id,
        actor_user_id=actor_user_id,
    )
    db.commit()


def _task_item(db: Session, task: TransportTask) -> dict[str, Any]:
    stops = list(db.scalars(select(TaskStop).where(TaskStop.task_id == task.id).order_by(TaskStop.sequence_no)))
    return {
        "id": task.id,
        "task_no": task.task_no,
        "source_order_ids": task.source_order_ids,
        "vehicle_id": task.vehicle_id,
        "driver_id": task.driver_id,
        "status": task.status,
        "status_label": enum_label(TRANSPORT_STATUS_LABELS, task.status),
        "temperature_zone": task.temperature_zone,
        "temperature_zone_label": enum_label(TEMPERATURE_ZONE_LABELS, task.temperature_zone),
        "total_weight_kg": task.total_weight_kg,
        "total_volume_m3": task.total_volume_m3,
        "planned_departure_at": task.planned_departure_at,
        "object_version": task.object_version,
        "route_label": "经纬度估算路线",
        "stops": [
            {
                "id": stop.id,
                "store_id": stop.store_id,
                "sequence_no": stop.sequence_no,
                "latitude": stop.latitude,
                "longitude": stop.longitude,
                "status": stop.status,
                "status_label": enum_label(TRANSPORT_STATUS_LABELS, stop.status),
                "object_version": stop.object_version,
                "delivery_lines": stop.delivery_lines,
            }
            for stop in stops
        ],
    }


def _mobile_task_filters(user: User):
    if user.role == "driver":
        return [TransportTask.driver_id == user.driver_id]
    if user.role in {"store_manager", "third_space_manager"}:
        return [TransportTask.id.in_(select(TaskStop.task_id).where(TaskStop.store_id == user.store_id))]
    if user.role == "park_admin":
        return []
    raise BusinessError("FORBIDDEN", "当前账号没有移动履约权限", status_code=403)


def verify_device_key(x_device_key: Annotated[str | None, Header(alias="X-Device-Key")] = None) -> None:
    if not x_device_key or x_device_key != get_settings().device_api_key:
        raise BusinessError("DEVICE_AUTH_FAILED", "设备凭证无效", status_code=401)


@app.get("/health/b02", tags=["健康检查"])
def health(request: Request, db: Session = Depends(get_db)) -> dict:
    db.execute(select(1))
    return api_payload(request, service="b02", status="ok", database="ok")


@app.post(f"{MOBILE}/auth/login", tags=["移动鉴权"])
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    return login_user(request, response, db, payload.username, payload.password, include_refresh=True)


@app.post(f"{MOBILE}/auth/wechat", tags=["移动鉴权"])
def wechat_login(
    payload: WechatLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not settings.is_production and payload.code.startswith("demo:"):
        user = db.scalar(select(User).where(User.username == payload.code.removeprefix("demo:")))
    else:
        if not settings.wechat_app_id or not settings.wechat_app_secret:
            raise BusinessError("WECHAT_NOT_CONFIGURED", "服务器尚未配置微信小程序登录", status_code=503)
        result = httpx.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": settings.wechat_app_id,
                "secret": settings.wechat_app_secret,
                "js_code": payload.code,
                "grant_type": "authorization_code",
            },
            timeout=8,
        ).json()
        openid = result.get("openid")
        if not openid:
            raise BusinessError(
                "WECHAT_LOGIN_FAILED",
                "微信登录凭证校验失败",
                status_code=401,
                details={"errcode": result.get("errcode")},
            )
        user = db.scalar(select(User).where(User.wechat_openid == openid))
    if not user or not user.active:
        raise BusinessError("WECHAT_ACCOUNT_UNBOUND", "微信身份尚未绑定业务账号", status_code=403)
    return issue_user_tokens(request, response, db, user, include_refresh=True)


@app.post(f"{MOBILE}/auth/refresh", tags=["移动鉴权"])
def refresh(payload: RefreshRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    return refresh_user(request, response, db, payload.refresh_token, include_refresh=True)


@app.get(f"{MOBILE}/auth/me", tags=["移动鉴权"])
def me(request: Request, user: User = Depends(get_current_user)) -> dict:
    return api_payload(
        request,
        user={
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "role_label": enum_label(ROLE_LABELS, user.role),
            "enterprise_id": user.enterprise_id,
            "store_id": user.store_id,
            "driver_id": user.driver_id,
            "object_version": user.object_version,
        },
        idle_timeout_seconds=get_settings().idle_timeout_minutes * 60,
    )


@app.post(f"{MOBILE}/auth/logout", tags=["移动鉴权"])
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    return logout_user(request, response, db)


@app.get(f"{MOBILE}/tasks", tags=["运输履约"])
def list_tasks(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    filters = _mobile_task_filters(user)
    if status:
        filters.append(TransportTask.status == status)
    total = db.scalar(select(func.count()).select_from(TransportTask).where(*filters)) or 0
    tasks = list(
        db.scalars(
            select(TransportTask)
            .where(*filters)
            .order_by(TransportTask.planned_departure_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return page_payload(request, [_task_item(db, item) for item in tasks], page=page, page_size=page_size, total=total)


@app.get(f"{MOBILE}/tasks/{{task_id}}", tags=["运输履约"])
def task_detail(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    task = db.scalar(select(TransportTask).where(TransportTask.id == task_id, *_mobile_task_filters(user)))
    if not task:
        raise BusinessError("TASK_NOT_FOUND", "运输任务不存在或无权查看", status_code=404)
    return api_payload(request, task=_task_item(db, task))


@app.post(f"{MOBILE}/tasks/{{task_id}}/actions", tags=["运输履约"])
def task_action(
    task_id: str,
    payload: TaskActionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("driver", "park_admin")),
) -> dict:
    task = db.get(TransportTask, task_id)
    if not task:
        raise BusinessError("TASK_NOT_FOUND", "运输任务不存在", status_code=404)
    result = transition_task(
        db,
        task=task,
        user=user,
        action=payload.action,
        expected_version=payload.object_version,
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
        payload=payload.model_dump(mode="json"),
    )
    return api_payload(request, **result)


@app.post(f"{MOBILE}/tasks/{{task_id}}/locations", status_code=201, tags=["运输履约"])
def submit_location(
    task_id: str,
    payload: LocationCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("driver")),
) -> dict:
    task = db.get(TransportTask, task_id)
    if not task:
        raise BusinessError("TASK_NOT_FOUND", "运输任务不存在", status_code=404)
    if user.driver_id != task.driver_id:
        _record_rejected_upload(
            db,
            task_id=task.id,
            task=task,
            source_type="DRIVER_LOCATION",
            issue_type="DRIVER_TASK_MISMATCH",
            idempotency_key=idempotency_key,
            message="位置上报司机与任务分配司机不一致",
            trace_id=request.state.trace_id,
            actual_driver_id=user.driver_id,
            actor_user_id=user.id,
        )
        raise BusinessError("DRIVER_TASK_MISMATCH", "只能上报分配给本人的运输任务位置", status_code=409)
    if task.status not in LOCATION_UPLOAD_STATUSES:
        _record_rejected_upload(
            db,
            task_id=task.id,
            task=task,
            source_type="DRIVER_LOCATION",
            issue_type="LOCATION_TASK_STATE_MISMATCH",
            idempotency_key=idempotency_key,
            message=f"任务状态 {task.status} 不允许上报位置",
            trace_id=request.state.trace_id,
            actual_driver_id=user.driver_id,
            actor_user_id=user.id,
        )
        raise BusinessError(
            "LOCATION_TASK_STATE_INVALID",
            "当前任务状态不允许上报位置",
            status_code=409,
            details={"current_status": task.status, "allowed_statuses": sorted(LOCATION_UPLOAD_STATUSES)},
        )
    result = create_location_point(
        db,
        task=task,
        user=user,
        payload=payload.model_dump(),
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
    )
    return api_payload(request, **result)


@app.post(f"{MOBILE}/tasks/{{task_id}}/exceptions", status_code=201, tags=["运输履约"])
def submit_task_exception(
    task_id: str,
    payload: TaskExceptionCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("driver", "park_admin")),
) -> dict:
    task = db.get(TransportTask, task_id)
    if not task:
        raise BusinessError("TASK_NOT_FOUND", "运输任务不存在", status_code=404)
    result = create_task_exception(
        db,
        task=task,
        user=user,
        payload=payload.model_dump(),
        idempotency_key=idempotency_key,
    )
    return api_payload(request, **result)


@app.post(f"{MOBILE}/tasks/{{task_id}}/receipts", status_code=201, tags=["签收与库存"])
def receipt(
    task_id: str,
    payload: ReceiptCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("store_manager", "third_space_manager", "park_admin")),
) -> dict:
    task = db.get(TransportTask, task_id)
    if not task:
        raise BusinessError("TASK_NOT_FOUND", "运输任务不存在", status_code=404)
    result = sign_receipt(
        db,
        task=task,
        user=user,
        receipt_status=payload.receipt_status,
        lines=[line.model_dump() for line in payload.lines],
        qr_token=payload.qr_token,
        expected_version=payload.object_version,
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
    )
    return api_payload(request, **result)


@app.get(f"{MOBILE}/inventory", tags=["签收与库存"])
def inventory(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("store_manager", "third_space_manager", "park_admin")),
) -> dict:
    store_id = user.store_id
    if user.role == "park_admin" and not store_id:
        rows = list(db.scalars(select(InventoryBalance)))
    elif store_id:
        rows = list(db.scalars(select(InventoryBalance).where(InventoryBalance.store_id == store_id)))
    else:
        rows = []
    return api_payload(
        request,
        items=[
            {
                "id": item.id,
                "store_id": item.store_id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "low_stock_threshold": item.low_stock_threshold,
                "object_version": item.object_version,
            }
            for item in rows
        ],
        page=1,
        page_size=len(rows),
        total=len(rows),
    )


@app.post(f"{MOBILE}/stockouts", status_code=201, tags=["第三空间经营"])
def submit_stockout(
    payload: StockoutCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("store_manager", "third_space_manager")),
) -> dict:
    result = create_stockout(
        db,
        user=user,
        product_id=payload.product_id,
        requested_quantity=payload.requested_quantity,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    return api_payload(request, **result)


@app.post(f"{MOBILE}/daily-reports", status_code=201, tags=["第三空间经营"])
def submit_daily_report(
    payload: DailyReportCreate,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("store_manager", "third_space_manager")),
) -> dict:
    result = create_daily_report(
        db,
        user=user,
        payload=payload.model_dump(),
        idempotency_key=idempotency_key,
    )
    return api_payload(request, **result)


@app.post("/api/v1/device/telemetry", status_code=202, tags=["设备遥测"])
def telemetry(
    payload: TelemetryBatch,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    _: None = Depends(verify_device_key),
) -> dict:
    task = db.get(TransportTask, payload.task_id)
    if not task:
        _record_rejected_upload(
            db,
            task_id=payload.task_id,
            source_type="DEVICE_TELEMETRY",
            issue_type="DEVICE_VEHICLE_MISMATCH",
            idempotency_key=idempotency_key,
            message="遥测上报引用了不存在的运输任务",
            trace_id=request.state.trace_id,
            actual_vehicle_id=payload.vehicle_id,
        )
        raise BusinessError("TELEMETRY_TASK_MISMATCH", "任务与遥测车辆不匹配", status_code=409)
    if task.vehicle_id != payload.vehicle_id:
        _record_rejected_upload(
            db,
            task_id=task.id,
            task=task,
            source_type="DEVICE_TELEMETRY",
            issue_type="DEVICE_VEHICLE_MISMATCH",
            idempotency_key=idempotency_key,
            message="遥测设备车辆与运输任务车辆不一致",
            trace_id=request.state.trace_id,
            actual_vehicle_id=payload.vehicle_id,
        )
        raise BusinessError("TELEMETRY_TASK_MISMATCH", "任务与遥测车辆不匹配", status_code=409)
    if task.status not in TELEMETRY_UPLOAD_STATUSES:
        _record_rejected_upload(
            db,
            task_id=task.id,
            task=task,
            source_type="DEVICE_TELEMETRY",
            issue_type="TELEMETRY_TASK_STATE_MISMATCH",
            idempotency_key=idempotency_key,
            message=f"任务状态 {task.status} 不允许上传温湿度遥测",
            trace_id=request.state.trace_id,
            actual_vehicle_id=payload.vehicle_id,
        )
        raise BusinessError(
            "TELEMETRY_TASK_STATE_INVALID",
            "当前任务状态不允许上传温湿度遥测",
            status_code=409,
            details={"current_status": task.status, "allowed_statuses": sorted(TELEMETRY_UPLOAD_STATUSES)},
        )
    result = ingest_telemetry(
        db,
        task,
        [sample.model_dump() for sample in payload.samples],
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
    )
    return api_payload(request, **result)


ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _valid_image_signature(content_type: str, content: bytes) -> bool:
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


@app.post(f"{MOBILE}/uploads", status_code=201, tags=["附件"])
async def upload_attachment(
    request: Request,
    purpose: str = Query(pattern="^(receipt|daily_report|stockout|alert)$"),
    file: UploadFile = File(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    settings = get_settings()
    suffix = ALLOWED_UPLOAD_TYPES.get(file.content_type or "")
    if suffix is None:
        raise BusinessError("UPLOAD_TYPE_NOT_ALLOWED", "只允许上传 JPG、PNG 或 WebP 图片", status_code=415)
    upload_root = Path(settings.upload_dir).resolve()
    upload_root.mkdir(parents=True, exist_ok=True)
    storage_name = f"{uuid4().hex}{suffix}"
    target = (upload_root / storage_name).resolve()
    if target.parent != upload_root:
        raise BusinessError("UPLOAD_PATH_INVALID", "附件存储路径无效", status_code=400)
    digest = hashlib.sha256()
    size = 0
    first_chunk = True
    try:
        with target.open("xb") as stream:
            while chunk := await file.read(1024 * 1024):
                if first_chunk and not _valid_image_signature(file.content_type or "", chunk):
                    raise BusinessError("UPLOAD_CONTENT_INVALID", "图片内容与文件类型不一致", status_code=415)
                first_chunk = False
                size += len(chunk)
                if size > settings.upload_max_bytes:
                    raise BusinessError("UPLOAD_TOO_LARGE", "附件大小不能超过 10 MB", status_code=413)
                digest.update(chunk)
                stream.write(chunk)
            if first_chunk:
                raise BusinessError("UPLOAD_EMPTY", "不能上传空文件", status_code=400)
    except Exception:
        if target.exists() and target.parent == upload_root:
            target.unlink()
        raise
    attachment = Attachment(
        owner_user_id=user.id,
        purpose=purpose,
        original_name=(file.filename or "upload")[:240],
        storage_name=storage_name,
        content_type=file.content_type,
        size_bytes=size,
        sha256=digest.hexdigest(),
    )
    db.add(attachment)
    db.commit()
    return api_payload(
        request,
        attachment={
            "id": attachment.id,
            "purpose": attachment.purpose,
            "content_type": attachment.content_type,
            "size_bytes": attachment.size_bytes,
            "sha256": attachment.sha256,
        },
    )


@app.get(f"{MOBILE}/uploads/{{attachment_id}}", tags=["附件"])
def download_attachment(
    attachment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    attachment = db.get(Attachment, attachment_id)
    if not attachment:
        raise BusinessError("ATTACHMENT_NOT_FOUND", "附件不存在", status_code=404)
    if user.role != "park_admin" and attachment.owner_user_id != user.id:
        raise BusinessError("FORBIDDEN", "无权读取该附件", status_code=403)
    upload_root = Path(get_settings().upload_dir).resolve()
    target = (upload_root / attachment.storage_name).resolve()
    if target.parent != upload_root or not target.is_file():
        raise BusinessError("ATTACHMENT_FILE_MISSING", "附件文件缺失", status_code=404)
    return FileResponse(target, media_type=attachment.content_type, filename=attachment.original_name)


@app.get(f"{MOBILE}/alerts", tags=["运输报警"])
def list_alerts(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    task_filters = _mobile_task_filters(user)
    query = select(Alert).where(Alert.task_id.in_(select(TransportTask.id).where(*task_filters)))
    if status:
        query = query.where(Alert.status == status)
    rows = list(db.scalars(query.order_by(Alert.opened_at.desc())))
    return api_payload(
        request,
        items=[
            {
                "id": item.id,
                "task_id": item.task_id,
                "alert_type": item.alert_type,
                "alert_type_label": enum_label(ALERT_TYPE_LABELS, item.alert_type),
                "status": item.status,
                "status_label": enum_label(ALERT_STATUS_LABELS, item.status),
                "message": item.message,
                "opened_at": item.opened_at,
                "object_version": item.object_version,
            }
            for item in rows
        ],
        page=1,
        page_size=len(rows),
        total=len(rows),
    )


@app.patch(f"{MOBILE}/alerts/{{alert_id}}", tags=["运输报警"])
def update_alert(
    alert_id: str,
    payload: AlertUpdate,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("driver", "park_admin")),
) -> dict:
    alert = db.get(Alert, alert_id)
    if not alert:
        raise BusinessError("ALERT_NOT_FOUND", "报警记录不存在", status_code=404)
    task = db.get(TransportTask, alert.task_id)
    if user.role == "driver" and task.driver_id != user.driver_id:
        raise BusinessError("FORBIDDEN", "只能处理本人运输任务的报警", status_code=403)
    request_body = payload.model_dump()
    effective_key = idempotency_key or f"auto-{payload.action}-{payload.object_version}"
    scope = f"alert-update:{alert.id}"
    cached = load_idempotent(db, scope, effective_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    if alert.object_version != payload.object_version:
        raise version_conflict(alert.object_version, payload.object_version)
    previous = alert.status
    if payload.action == "ACKNOWLEDGE" and alert.status == "OPEN":
        next_status = "ACKNOWLEDGED"
        changes = {"status": next_status, "acknowledged_at": utcnow(), "actor_user_id": user.id}
        allowed_statuses = ("OPEN",)
    elif payload.action == "RESOLVE" and alert.status in {"OPEN", "ACKNOWLEDGED"}:
        next_status = "RESOLVED"
        changes = {"status": next_status, "resolved_at": utcnow(), "actor_user_id": user.id}
        allowed_statuses = ("OPEN", "ACKNOWLEDGED")
    else:
        raise BusinessError("INVALID_TRANSITION", "当前报警状态不能执行该操作", status_code=409)
    next_alert_version = atomic_versioned_update(
        db,
        Alert,
        alert.id,
        payload.object_version,
        changes,
        conditions=(Alert.status.in_(allowed_statuses),),
    )
    response_body = {
        "id": alert.id,
        "status": next_status,
        "status_label": enum_label(ALERT_STATUS_LABELS, next_status),
        "action_label": enum_label(TRANSPORT_ACTION_LABELS, payload.action),
        "object_version": next_alert_version,
    }
    add_outbox(db, "transport.alert.status_changed", "alert", alert.id, next_alert_version, response_body)
    add_audit(
        db, request.state.trace_id, user.id, payload.action, "alert", alert.id, {"status": previous}, response_body
    )
    save_idempotent(db, scope, effective_key, request_body, response_body)
    db.commit()
    return api_payload(request, **response_body)


def _event_visible(db: Session, user: User, event: OutboxEvent) -> bool:
    if user.role == "park_admin":
        return True
    task_id: str | None = None
    if event.object_type == "transport_task":
        task_id = event.object_id
    elif event.object_type == "alert":
        alert = db.get(Alert, event.object_id)
        task_id = alert.task_id if alert else None
    elif event.object_type == "receipt":
        receipt = db.get(Receipt, event.object_id)
        return bool(receipt and user.store_id == receipt.store_id)
    elif event.object_type == "stockout_request":
        stockout = db.get(StockoutRequest, event.object_id)
        return bool(stockout and user.store_id == stockout.store_id)
    elif event.object_type == "store_daily_report":
        report = db.get(StoreDailyReport, event.object_id)
        return bool(report and user.store_id == report.store_id)
    elif event.object_type == "telemetry_issue":
        issue = db.get(TelemetryIssue, event.object_id)
        task_id = issue.task_id if issue else None
    if task_id:
        task = db.get(TransportTask, task_id)
        if not task:
            return False
        if user.role == "driver":
            return task.driver_id == user.driver_id
        if user.store_id:
            return bool(
                db.scalar(select(TaskStop.id).where(TaskStop.task_id == task.id, TaskStop.store_id == user.store_id))
            )
    return False


def websocket_session_error(db: Session, payload: dict[str, Any]) -> str | None:
    now = utcnow()
    user = db.get(User, payload.get("sub"))
    session = db.get(UserSession, payload.get("sid"))
    if not user or not user.active or not session or session.user_id != user.id:
        return "SESSION_INVALID"
    if session.revoked_at is not None:
        return "SESSION_REVOKED"
    if payload.get("ver") != user.session_version or session.session_version != user.session_version:
        return "SESSION_REPLACED"
    try:
        if float(payload.get("exp")) <= now.timestamp():
            return "TOKEN_EXPIRED"
    except (TypeError, ValueError):
        return "TOKEN_INVALID"
    if as_utc(session.expires_at) <= now:
        return "SESSION_EXPIRED"
    idle_limit = timedelta(minutes=get_settings().idle_timeout_minutes)
    if now - as_utc(session.last_activity_at) > idle_limit:
        return "SESSION_TIMEOUT"
    return None


@app.websocket(f"{MOBILE}/ws")
async def mobile_events(websocket: WebSocket, token: str, cursor: int = 0) -> None:
    try:
        payload = decode_token(token, "access")
        with SessionLocal() as db:
            session_error = websocket_session_error(db, payload)
            if session_error:
                await websocket.close(code=4401)
                return
        await websocket.accept()
        loop = asyncio.get_running_loop()
        last_session_check = loop.time()
        while True:
            with SessionLocal() as db:
                if loop.time() - last_session_check >= WS_SESSION_CHECK_INTERVAL_SECONDS:
                    session_error = websocket_session_error(db, payload)
                    last_session_check = loop.time()
                    if session_error:
                        await websocket.close(code=4401, reason=session_error)
                        return
                user = db.get(User, payload.get("sub"))
                events = list(
                    db.scalars(
                        select(OutboxEvent)
                        .where(OutboxEvent.sequence > cursor)
                        .order_by(OutboxEvent.sequence)
                        .limit(100)
                    )
                )
                visible = [event for event in events if _event_visible(db, user, event)]
            for event in visible:
                cursor = max(cursor, event.sequence)
                await websocket.send_text(
                    json.dumps(
                        {
                            "cursor": event.sequence,
                            "event_id": event.event_id,
                            "topic": event.topic,
                            "object_type": event.object_type,
                            "object_id": event.object_id,
                            "object_version": event.object_version,
                            "payload": event.payload,
                            "created_at": event.created_at.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                )
            if events:
                cursor = max(cursor, max(event.sequence for event in events))
            await websocket.send_json({"type": "heartbeat", "cursor": cursor})
            await asyncio.sleep(WS_HEARTBEAT_INTERVAL_SECONDS)
    except (BusinessError, WebSocketDisconnect):
        return
