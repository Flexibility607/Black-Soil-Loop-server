from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.algorithms import forecast_demand, run_carpool, run_procurement, run_warehouse_pool
from app.domain.assistant import answer_data_question
from app.domain.dashboard import read_dashboard_projection, refresh_dashboard_projection
from app.domain.openai_layer import transcribe_audio
from app.domain.services import add_audit, add_outbox, load_idempotent, save_idempotent
from app.shared.auth_api import login_user, logout_user, refresh_user
from app.shared.config import get_settings
from app.shared.database import SessionLocal, engine, get_db, init_database
from app.shared.dependencies import get_current_user, require_roles
from app.shared.errors import BusinessError, version_conflict
from app.shared.http import configure_app
from app.shared.models import (
    AlgorithmRun,
    Enterprise,
    OutboxEvent,
    Product,
    Store,
    Supplier,
    TransportOrder,
    TransportPlan,
    User,
    Vehicle,
    Warehouse,
    utcnow,
)
from app.shared.responses import api_payload, page_payload
from app.shared.schemas import (
    AssistantQuery,
    CancelRequest,
    LoginRequest,
    MatchConfirmRequest,
    MatchPreviewRequest,
    RefreshRequest,
    TransportOrderCreate,
    VersionedRequest,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if engine.dialect.name == "sqlite":
        init_database()
    yield


app = configure_app(
    FastAPI(
        title="黑土循环 B01 网页与规划服务",
        version="0.1.0",
        description="网页、公开大屏、主数据、拼车拼仓、采购、预测与看板 API",
        lifespan=lifespan,
    )
)

WEB = "/api/v1/web"


def _model_item(item: Any, fields: list[str]) -> dict[str, Any]:
    return {field: getattr(item, field) for field in fields}


@app.get("/health/b01", tags=["健康检查"])
def health(request: Request, db: Session = Depends(get_db)) -> dict:
    db.execute(select(1))
    return api_payload(request, service="b01", status="ok", database="ok")


@app.post(f"{WEB}/auth/login", tags=["网页鉴权"])
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    return login_user(request, response, db, payload.username, payload.password, include_refresh=False)


@app.post(f"{WEB}/auth/refresh", tags=["网页鉴权"])
def refresh(payload: RefreshRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    return refresh_user(request, response, db, payload.refresh_token, include_refresh=False)


@app.get(f"{WEB}/auth/me", tags=["网页鉴权"])
def me(request: Request, user: User = Depends(get_current_user)) -> dict:
    return api_payload(
        request,
        user=_model_item(
            user,
            ["id", "username", "display_name", "role", "enterprise_id", "store_id", "driver_id", "object_version"],
        ),
        idle_timeout_seconds=get_settings().idle_timeout_minutes * 60,
    )


@app.post(f"{WEB}/auth/logout", tags=["网页鉴权"])
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    return logout_user(request, response, db)


@app.get(f"{WEB}/master-data/{{resource}}", tags=["主数据"])
def master_data(
    resource: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    resources: dict[str, tuple[type, list[str]]] = {
        "enterprises": (Enterprise, ["id", "code", "name", "enabled", "object_version"]),
        "stores": (
            Store,
            ["id", "enterprise_id", "code", "name", "channel", "latitude", "longitude", "enabled", "object_version"],
        ),
        "products": (Product, ["id", "code", "name", "category", "unit", "temperature_zone", "object_version"]),
        "vehicles": (
            Vehicle,
            [
                "id",
                "driver_id",
                "plate_no",
                "temperature_zone",
                "max_weight_kg",
                "max_volume_m3",
                "enabled",
                "object_version",
            ],
        ),
        "warehouses": (
            Warehouse,
            [
                "id",
                "code",
                "name",
                "temperature_zone",
                "latitude",
                "longitude",
                "capacity_m3",
                "used_m3",
                "object_version",
            ],
        ),
        "suppliers": (Supplier, ["id", "code", "name", "delivery_score", "quality_score", "object_version"]),
    }
    if resource not in resources:
        raise BusinessError("RESOURCE_NOT_FOUND", "未知主数据资源", status_code=404, details={"resource": resource})
    model, fields = resources[resource]
    total = db.scalar(select(func.count()).select_from(model)) or 0
    items = list(db.scalars(select(model).offset((page - 1) * page_size).limit(page_size)))
    return page_payload(
        request, [_model_item(item, fields) for item in items], page=page, page_size=page_size, total=total
    )


@app.get(f"{WEB}/transport/orders", tags=["运输计划"])
def list_orders(
    request: Request,
    scenario_code: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    filters = [TransportOrder.scenario_code == scenario_code] if scenario_code else []
    total = db.scalar(select(func.count()).select_from(TransportOrder).where(*filters)) or 0
    rows = list(
        db.scalars(
            select(TransportOrder)
            .where(*filters)
            .order_by(TransportOrder.departure_at, TransportOrder.order_no)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    fields = [
        "id",
        "order_no",
        "scenario_code",
        "enterprise_id",
        "product_id",
        "store_id",
        "departure_at",
        "quantity",
        "unit",
        "weight_kg",
        "volume_m3",
        "temperature_zone",
        "status",
        "object_version",
    ]
    return page_payload(
        request, [_model_item(item, fields) for item in rows], page=page, page_size=page_size, total=total
    )


@app.post(f"{WEB}/transport/orders", status_code=201, tags=["运输计划"])
def create_order(
    payload: TransportOrderCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    if db.scalar(select(TransportOrder.id).where(TransportOrder.order_no == payload.order_no)):
        raise BusinessError("ORDER_NO_EXISTS", "运输订单号已存在", status_code=409)
    if user.role == "enterprise_admin" and user.enterprise_id != payload.enterprise_id:
        raise BusinessError("FORBIDDEN", "只能为本企业创建运输订单", status_code=403)
    order = TransportOrder(**payload.model_dump())
    db.add(order)
    db.flush()
    add_outbox(
        db, "transport.order.created", "transport_order", order.id, order.object_version, {"order_no": order.order_no}
    )
    db.commit()
    return api_payload(request, order=_model_item(order, ["id", "order_no", "status", "object_version"]))


def _selected_orders(db: Session, payload: MatchPreviewRequest) -> list[TransportOrder]:
    query = select(TransportOrder)
    if payload.order_ids:
        query = query.where(TransportOrder.id.in_(payload.order_ids))
    elif payload.scenario_code:
        query = query.where(TransportOrder.scenario_code == payload.scenario_code)
    else:
        query = query.where(TransportOrder.status == "DRAFT")
    return list(db.scalars(query.order_by(TransportOrder.departure_at)))


@app.post(f"{WEB}/algorithms/carpool/preview", tags=["拼车算法"])
def carpool_preview(
    payload: MatchPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    orders = _selected_orders(db, payload)
    result = run_carpool(orders, list(db.scalars(select(Vehicle))))
    run = AlgorithmRun(
        algorithm_type="CARPOOL",
        scenario_code=payload.scenario_code,
        rules_version=result["rules_version"],
        input_snapshot={"order_ids": [item.id for item in orders]},
        output_snapshot=result,
    )
    db.add(run)
    db.commit()
    return api_payload(request, match_run_id=run.id, **result)


@app.post(f"{WEB}/algorithms/warehouse-pool/preview", tags=["拼仓算法"])
def warehouse_preview(
    payload: MatchPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    orders = _selected_orders(db, payload)
    result = run_warehouse_pool(orders, list(db.scalars(select(Warehouse))))
    run = AlgorithmRun(
        algorithm_type="WAREHOUSE",
        scenario_code=payload.scenario_code,
        rules_version=result["rules_version"],
        input_snapshot={"order_ids": [item.id for item in orders]},
        output_snapshot=result,
    )
    db.add(run)
    db.commit()
    return api_payload(request, match_run_id=run.id, **result)


@app.post(f"{WEB}/algorithms/carpool/runs/{{run_id}}/confirm", status_code=201, tags=["拼车算法"])
def confirm_carpool(
    run_id: str,
    payload: MatchConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    if payload.match_run_id != run_id:
        raise BusinessError("RUN_ID_MISMATCH", "请求中的算法运行编号不一致", status_code=409)
    run = db.get(AlgorithmRun, run_id)
    if not run or run.algorithm_type != "CARPOOL":
        raise BusinessError("MATCH_RUN_NOT_FOUND", "拼车计算记录不存在", status_code=404)
    if run.confirmed:
        raise BusinessError("MATCH_RUN_CONFIRMED", "该拼车方案已经确认", status_code=409)
    candidates = run.output_snapshot.get("candidates", [])
    if payload.candidate_index >= len(candidates):
        raise BusinessError("CANDIDATE_NOT_FOUND", "候选方案不存在", status_code=404)
    candidate = candidates[payload.candidate_index]
    orders = list(db.scalars(select(TransportOrder).where(TransportOrder.id.in_(candidate["order_ids"]))))
    stale = [order.order_no for order in orders if order.object_version != payload.object_version]
    if stale:
        raise version_conflict(max(order.object_version for order in orders))
    if any(order.status != "DRAFT" for order in orders):
        raise BusinessError("ORDER_STATE_CHANGED", "候选订单状态已经变化，请重新计算", status_code=409)

    plan = TransportPlan(
        plan_no=f"PLAN-{utcnow().strftime('%y%m%d%H%M%S')}-{run.id[:4].upper()}",
        algorithm_run_id=run.id,
        candidate_snapshot=candidate,
        status="CONFIRMED",
    )
    db.add(plan)
    db.flush()
    for order in orders:
        order.status = "CONFIRMED"
        order.object_version += 1
    run.confirmed = True
    add_outbox(
        db,
        "transport.plan.confirmed",
        "transport_plan",
        plan.id,
        plan.object_version,
        {
            "plan_id": plan.id,
            "plan_no": plan.plan_no,
            "candidate": candidate,
            "actor_user_id": user.id,
        },
    )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "CONFIRM_CARPOOL",
        "transport_plan",
        plan.id,
        None,
        {"status": plan.status},
    )
    db.commit()
    return api_payload(
        request,
        plan=_model_item(plan, ["id", "plan_no", "status", "object_version", "task_id"]),
    )


@app.get(f"{WEB}/transport/plans/{{plan_id}}", tags=["运输计划"])
def plan_detail(
    plan_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin")),
) -> dict:
    plan = db.get(TransportPlan, plan_id)
    if not plan:
        raise BusinessError("PLAN_NOT_FOUND", "运输计划不存在", status_code=404)
    return api_payload(
        request,
        plan=_model_item(
            plan,
            [
                "id",
                "plan_no",
                "status",
                "object_version",
                "task_id",
                "candidate_snapshot",
                "published_qr_token",
                "qr_expires_at",
                "cancellation_reason",
                "last_error",
            ],
        ),
    )


@app.get(f"{WEB}/transport/plans", tags=["运输计划"])
def list_plans(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    total = db.scalar(select(func.count()).select_from(TransportPlan)) or 0
    plans = list(
        db.scalars(
            select(TransportPlan)
            .order_by(TransportPlan.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    fields = ["id", "plan_no", "status", "object_version", "task_id", "created_at", "updated_at"]
    return page_payload(
        request,
        [_model_item(plan, fields) for plan in plans],
        page=page,
        page_size=page_size,
        total=total,
    )


def _projection_items(db: Session, group: str) -> tuple[list[dict], Any]:
    snapshot = refresh_dashboard_projection(db) if not get_settings().is_production else read_dashboard_projection(db)
    if snapshot is None:
        raise BusinessError("DASHBOARD_NOT_READY", "看板投影尚未生成", status_code=503)
    cutoff = snapshot["data_cutoff"]
    if group == "tasks":
        return list(snapshot.get("map", {}).get("routes", [])), cutoff
    return list(snapshot.get("alerts", [])), cutoff


@app.get(f"{WEB}/transport/tasks", tags=["运输任务投影"])
def projected_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    items, cutoff = _projection_items(db, "tasks")
    start = (page - 1) * page_size
    payload = page_payload(request, items[start : start + page_size], page=page, page_size=page_size, total=len(items))
    payload["data_cutoff"] = cutoff.isoformat() if hasattr(cutoff, "isoformat") else str(cutoff)
    return payload


@app.get(f"{WEB}/alerts", tags=["运输异常投影"])
def projected_alerts(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    items, cutoff = _projection_items(db, "alerts")
    start = (page - 1) * page_size
    payload = page_payload(request, items[start : start + page_size], page=page, page_size=page_size, total=len(items))
    payload["data_cutoff"] = cutoff.isoformat() if hasattr(cutoff, "isoformat") else str(cutoff)
    return payload


@app.post(f"{WEB}/transport/plans/{{plan_id}}/publish", status_code=202, tags=["运输计划"])
def publish_plan(
    plan_id: str,
    payload: VersionedRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    plan = db.get(TransportPlan, plan_id)
    if not plan:
        raise BusinessError("PLAN_NOT_FOUND", "运输计划不存在", status_code=404)
    request_body = payload.model_dump()
    cached = load_idempotent(db, f"publish-plan:{plan_id}", idempotency_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    if plan.object_version != payload.object_version:
        raise version_conflict(plan.object_version)
    if plan.status != "READY" or not plan.task_id:
        raise BusinessError("PLAN_NOT_READY", "执行任务尚未建立，请稍后刷新", status_code=409)
    plan.status = "PUBLISH_REQUESTED"
    plan.object_version += 1
    response_body = {
        "plan_id": plan.id,
        "task_id": plan.task_id,
        "status": plan.status,
        "object_version": plan.object_version,
    }
    add_outbox(
        db,
        "transport.plan.publish_requested",
        "transport_plan",
        plan.id,
        plan.object_version,
        {**response_body, "actor_user_id": user.id, "idempotency_key": idempotency_key},
    )
    save_idempotent(db, f"publish-plan:{plan_id}", idempotency_key, request_body, response_body, 202)
    db.commit()
    return api_payload(request, **response_body)


@app.post(f"{WEB}/transport/plans/{{plan_id}}/cancel", status_code=202, tags=["运输计划"])
def cancel_plan(
    plan_id: str,
    payload: CancelRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    plan = db.get(TransportPlan, plan_id)
    if not plan:
        raise BusinessError("PLAN_NOT_FOUND", "运输计划不存在", status_code=404)
    request_body = payload.model_dump()
    scope = f"cancel-plan:{plan.id}"
    cached = load_idempotent(db, scope, idempotency_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    if plan.object_version != payload.object_version:
        raise version_conflict(plan.object_version)
    if plan.status not in {"CONFIRMED", "READY", "PUBLISHED"}:
        raise BusinessError("INVALID_TRANSITION", "当前运输计划不能申请取消", status_code=409)
    plan.status = "CANCELLATION_REQUESTED"
    plan.cancellation_reason = payload.reason
    plan.last_error = None
    plan.object_version += 1
    response_body = {
        "plan_id": plan.id,
        "task_id": plan.task_id,
        "status": plan.status,
        "object_version": plan.object_version,
    }
    add_outbox(
        db,
        "transport.plan.cancellation_requested",
        "transport_plan",
        plan.id,
        plan.object_version,
        {
            **response_body,
            "reason": payload.reason,
            "actor_user_id": user.id,
            "idempotency_key": idempotency_key,
        },
    )
    save_idempotent(db, scope, idempotency_key, request_body, response_body, 202)
    db.commit()
    return api_payload(request, **response_body)


@app.get(f"{WEB}/algorithms/procurement", tags=["采购推荐"])
def procurement(
    request: Request,
    product_id: str,
    required_quantity: float = Query(gt=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    result = run_procurement(db, product_id, required_quantity, date.today())
    run = AlgorithmRun(
        algorithm_type="PROCUREMENT",
        rules_version=result["rules_version"],
        input_snapshot={"product_id": product_id, "required_quantity": required_quantity},
        output_snapshot=result,
    )
    db.add(run)
    db.commit()
    return api_payload(request, run_id=run.id, **result)


@app.get(f"{WEB}/algorithms/forecast", tags=["需求预测"])
def forecast(
    enterprise_id: str,
    product_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    result = forecast_demand(db, enterprise_id, product_id, date.today())
    run = AlgorithmRun(
        algorithm_type="DEMAND_FORECAST",
        rules_version="forecast-auto-v1",
        input_snapshot={"enterprise_id": enterprise_id, "product_id": product_id},
        output_snapshot=result,
    )
    db.add(run)
    db.commit()
    return api_payload(request, run_id=run.id, data_cutoff=result["data_cutoff"], forecast=result)


@app.get(f"{WEB}/dashboard/snapshot", tags=["网页看板"])
def web_snapshot(
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    snapshot = refresh_dashboard_projection(db) if not get_settings().is_production else read_dashboard_projection(db)
    if snapshot is None:
        raise BusinessError("DASHBOARD_NOT_READY", "看板投影尚未生成", status_code=503)
    cutoff = snapshot.pop("data_cutoff")
    return api_payload(request, data_cutoff=cutoff, **snapshot)


@app.get("/api/v1/public/dashboard/snapshot", tags=["公开大屏"])
def public_snapshot(request: Request, db: Session = Depends(get_db)) -> dict:
    snapshot = refresh_dashboard_projection(db) if not get_settings().is_production else read_dashboard_projection(db)
    if snapshot is None:
        raise BusinessError("DASHBOARD_NOT_READY", "看板投影尚未生成", status_code=503)
    cutoff = snapshot.pop("data_cutoff")
    return api_payload(request, data_cutoff=cutoff, **snapshot)


@app.post(f"{WEB}/assistant/query", tags=["数据助手"])
def assistant_query(
    payload: AssistantQuery,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    if not get_settings().is_production:
        refresh_dashboard_projection(db)
    result = answer_data_question(db, payload.question, payload.preferred_chart)
    cutoff = result.pop("data_cutoff")
    return api_payload(request, data_cutoff=cutoff, **result)


@app.post("/api/v1/public/assistant/query", tags=["公开数据助手"])
def public_assistant_query(payload: AssistantQuery, request: Request, db: Session = Depends(get_db)) -> dict:
    if not get_settings().is_production:
        refresh_dashboard_projection(db)
    result = answer_data_question(db, payload.question, payload.preferred_chart)
    cutoff = result.pop("data_cutoff")
    return api_payload(request, data_cutoff=cutoff, **result)


@app.post(f"{WEB}/assistant/transcriptions", tags=["数据助手"])
async def assistant_transcription(
    request: Request,
    audio: UploadFile = File(...),
    duration_seconds: float = Form(..., gt=0, le=30),
    _: User = Depends(get_current_user),
) -> dict:
    allowed_types = {"audio/webm", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg"}
    content_type = (audio.content_type or "").lower()
    if content_type not in allowed_types:
        raise BusinessError("AUDIO_TYPE_NOT_ALLOWED", "仅支持 WebM、MP4、MP3、WAV 或 OGG 录音", status_code=415)
    maximum = min(get_settings().upload_max_bytes, 10 * 1024 * 1024)
    content = await audio.read(maximum + 1)
    if len(content) > maximum:
        raise BusinessError("AUDIO_TOO_LARGE", "录音文件不能超过 10 MB", status_code=413)
    transcript = await transcribe_audio(audio.filename or "question.webm", content_type, content)
    return api_payload(request, transcript=transcript, duration_seconds=duration_seconds)


@app.get(f"{WEB}/dictionaries", tags=["公共约定"])
def dictionaries(request: Request, _: User = Depends(get_current_user)) -> dict:
    return api_payload(
        request,
        dictionaries={
            "transport_status": {
                "DRAFT": "草稿",
                "MATCHED": "已匹配",
                "CONFIRMED": "已确认",
                "PUBLISHED": "已发布",
                "DRIVER_ACCEPTED": "司机已接单",
                "PICKED_UP": "已取货",
                "IN_TRANSIT": "运输中",
                "DELIVERED": "已送达",
                "STORE_SIGNED": "门店已签收",
                "COMPLETED": "已完成",
                "CANCELLED": "已取消",
            },
            "temperature_zone": {"AMBIENT": "常温", "CHILLED": "冷藏", "FROZEN": "冷冻"},
            "channel": {"TRADITIONAL": "传统门店", "THIRD_SPACE": "第三空间"},
            "receipt_status": {"FULL": "全量签收", "PARTIAL": "差异签收", "REJECTED": "拒收"},
        },
    )


@app.get("/api/v1/dashboard/events", tags=["实时事件"])
async def dashboard_events(request: Request, cursor: int = Query(0, ge=0)) -> StreamingResponse:
    header_cursor = request.headers.get("Last-Event-ID")
    if header_cursor and header_cursor.isdigit():
        cursor = max(cursor, int(header_cursor))

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with SessionLocal() as db:
                events = list(
                    db.scalars(
                        select(OutboxEvent)
                        .where(OutboxEvent.sequence > cursor)
                        .order_by(OutboxEvent.sequence)
                        .limit(100)
                    )
                )
            if events:
                for event in events:
                    cursor = event.sequence
                    body = {
                        "event_id": event.event_id,
                        "topic": event.topic,
                        "object_type": event.object_type,
                        "object_id": event.object_id,
                        "object_version": event.object_version,
                        "payload": event.payload,
                        "created_at": event.created_at.isoformat(),
                    }
                    serialized = json.dumps(body, ensure_ascii=False)
                    yield f"id: {event.sequence}\nevent: {event.topic}\ndata: {serialized}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
