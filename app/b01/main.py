from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.domain.algorithm_presentation import carpool_presentation, warehouse_presentation
from app.domain.algorithms import forecast_demand, run_carpool, run_procurement, run_warehouse_pool
from app.domain.assistant import answer_data_question
from app.domain.dashboard import read_dashboard_projection, refresh_dashboard_projection
from app.domain.dashboard_information import public_information
from app.domain.forecasts import (
    forecast_projection_data,
    generate_next_week_forecasts,
    next_week_window,
)
from app.domain.operations import (
    operations_summary,
    scoped_store_ids,
)
from app.domain.procurement import (
    aggregation_data,
    generate_procurement_aggregations,
    procurement_cycle,
)
from app.domain.services import add_audit, add_outbox, load_idempotent, save_idempotent
from app.domain.voice_limits import anonymous_subject_hash, claim_public_text_quota
from app.domain.voice_service import cleanup_expired_voice_requests, transcribe_voice_request
from app.shared.auth_api import login_user, logout_user, refresh_user
from app.shared.config import get_settings
from app.shared.database import SessionLocal, engine, get_db, init_database
from app.shared.dependencies import get_current_user, require_roles
from app.shared.dictionaries import (
    ALGORITHM_TYPE_LABELS,
    FORECAST_METHOD_LABELS,
    INVENTORY_QUANTITY_CONTEXT_LABELS,
    MOVEMENT_TYPE_LABELS,
    RECEIPT_STATUS_LABELS,
    STOCKOUT_STATUS_LABELS,
    TELEMETRY_ISSUE_STATUS_LABELS,
    TELEMETRY_ISSUE_TYPE_LABELS,
    dictionary_payload,
    enum_label,
)
from app.shared.errors import BusinessError, version_conflict
from app.shared.http import configure_app
from app.shared.models import (
    AlgorithmRun,
    DemandForecastBatch,
    DemandForecastProjection,
    Enterprise,
    InventoryMovementProjection,
    OutboxEvent,
    ProcurementAggregation,
    ProcurementDemandConfirmation,
    Product,
    ReceiptProjection,
    StockoutDemandProjection,
    Store,
    StoreDailyReportProjection,
    Supplier,
    TelemetryIssueProjection,
    TransportOrder,
    TransportPlan,
    User,
    Vehicle,
    Warehouse,
    WarehousePoolPlan,
    WarehouseReservation,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update
from app.shared.periods import period_window
from app.shared.responses import api_payload, page_payload
from app.shared.schemas import (
    AssistantQuery,
    CancelRequest,
    LoginRequest,
    MatchConfirmRequest,
    MatchPreviewRequest,
    ProcurementAggregationConfirmRequest,
    ProcurementDemandConfirmRequest,
    ProcurementGenerateRequest,
    RefreshRequest,
    TransportOrderCreate,
    VersionedRequest,
    WarehousePoolConfirmRequest,
)
from app.shared.security import as_utc


@asynccontextmanager
async def lifespan(_: FastAPI):
    if engine.dialect.name == "sqlite":
        init_database()

    async def cleanup_voice_replays() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                with SessionLocal() as cleanup_db:
                    cleanup_expired_voice_requests(cleanup_db)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("voice replay cleanup failed: %s", type(exc).__name__)

    cleanup_task = asyncio.create_task(cleanup_voice_replays())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = configure_app(
    FastAPI(
        title="黑土循环 B01 网页与规划服务",
        version="0.1.0",
        description="网页、公开大屏、主数据、拼车拼仓、采购、预测与看板 API",
        lifespan=lifespan,
    )
)

WEB = "/api/v1/web"
logger = logging.getLogger(__name__)


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
                "reserved_m3",
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
        "warehouse_inbound_start",
        "warehouse_inbound_end",
        "warehouse_outbound_start",
        "warehouse_outbound_end",
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
    _: User = Depends(require_roles("park_admin")),
) -> dict:
    orders = _selected_orders(db, payload)
    vehicles = list(db.scalars(select(Vehicle)))
    result = run_carpool(orders, vehicles)
    run = AlgorithmRun(
        algorithm_type="CARPOOL",
        scenario_code=payload.scenario_code,
        rules_version=result["rules_version"],
        input_snapshot={
            "order_ids": [item.id for item in orders],
            "order_versions": {item.id: item.object_version for item in orders},
        },
        output_snapshot=result,
    )
    db.add(run)
    db.flush()
    add_outbox(
        db,
        "algorithm.run.completed",
        "algorithm_run",
        run.id,
        1,
        {"algorithm_type": "CARPOOL", "showcase": False},
    )
    db.commit()
    presentation = carpool_presentation(db, orders, vehicles, result)
    return api_payload(
        request,
        match_run_id=run.id,
        algorithm_type="CARPOOL",
        algorithm_type_label=enum_label(ALGORITHM_TYPE_LABELS, "CARPOOL"),
        presentation=presentation,
        **result,
    )


@app.post(f"{WEB}/algorithms/warehouse-pool/preview", tags=["拼仓算法"])
def warehouse_preview(
    payload: MatchPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    orders = _selected_orders(db, payload)
    warehouses = list(db.scalars(select(Warehouse)))
    result = run_warehouse_pool(orders, warehouses)
    run = AlgorithmRun(
        algorithm_type="WAREHOUSE",
        scenario_code=payload.scenario_code,
        rules_version=result["rules_version"],
        input_snapshot={
            "order_ids": [item.id for item in orders],
            "order_versions": {item.id: item.object_version for item in orders},
        },
        output_snapshot=result,
    )
    db.add(run)
    db.flush()
    add_outbox(
        db,
        "algorithm.run.completed",
        "algorithm_run",
        run.id,
        1,
        {"algorithm_type": "WAREHOUSE", "showcase": False},
    )
    db.commit()
    presentation = warehouse_presentation(orders, warehouses, result)
    return api_payload(
        request,
        match_run_id=run.id,
        algorithm_type="WAREHOUSE",
        algorithm_type_label=enum_label(ALGORITHM_TYPE_LABELS, "WAREHOUSE"),
        presentation=presentation,
        **result,
    )


def _warehouse_plan_data(db: Session, plan: WarehousePoolPlan) -> dict[str, Any]:
    reservation = db.scalar(select(WarehouseReservation).where(WarehouseReservation.warehouse_pool_plan_id == plan.id))
    warehouse = db.get(Warehouse, plan.warehouse_id)
    return {
        **_model_item(
            plan,
            [
                "id",
                "plan_no",
                "algorithm_run_id",
                "warehouse_id",
                "order_ids",
                "required_volume_m3",
                "inbound_start",
                "inbound_end",
                "outbound_start",
                "outbound_end",
                "status",
                "reservation_expires_at",
                "cancellation_reason",
                "last_error",
                "object_version",
                "created_at",
                "updated_at",
            ],
        ),
        "warehouse": (
            _model_item(
                warehouse,
                ["id", "code", "name", "capacity_m3", "used_m3", "reserved_m3", "object_version"],
            )
            if warehouse
            else None
        ),
        "reservation": (
            _model_item(
                reservation,
                [
                    "id",
                    "reserved_volume_m3",
                    "status",
                    "expires_at",
                    "occupied_at",
                    "released_at",
                    "object_version",
                ],
            )
            if reservation
            else None
        ),
    }


@app.post(f"{WEB}/algorithms/warehouse-pool/runs/{{run_id}}/confirm", status_code=202, tags=["拼仓算法"])
def confirm_warehouse_pool(
    run_id: str,
    payload: WarehousePoolConfirmRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    request_body = payload.model_dump()
    scope = f"confirm-warehouse-pool:{run_id}"
    cached = load_idempotent(db, scope, idempotency_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    if payload.match_run_id != run_id:
        raise BusinessError("RUN_ID_MISMATCH", "请求中的算法运行编号不一致", status_code=409)
    run = db.get(AlgorithmRun, run_id)
    if not run or run.algorithm_type != "WAREHOUSE":
        raise BusinessError("MATCH_RUN_NOT_FOUND", "拼仓计算记录不存在", status_code=404)
    if run.confirmed:
        raise BusinessError("MATCH_RUN_CONFIRMED", "该拼仓方案已经确认", status_code=409)
    candidates = run.output_snapshot.get("candidates", [])
    if payload.candidate_index >= len(candidates):
        raise BusinessError("CANDIDATE_NOT_FOUND", "拼仓候选方案不存在", status_code=404)
    candidate = candidates[payload.candidate_index]
    warehouse_candidate = next(
        (
            item
            for item in candidate.get("candidate_warehouses", [])
            if item.get("warehouse_id") == payload.warehouse_id
        ),
        None,
    )
    if warehouse_candidate is None:
        raise BusinessError("WAREHOUSE_NOT_IN_CANDIDATE", "所选仓库不属于该候选方案", status_code=409)
    if not warehouse_candidate.get("eligible"):
        raise BusinessError(
            "WAREHOUSE_NOT_ELIGIBLE",
            "所选仓库不满足拼仓约束",
            status_code=409,
            details={"reasons": warehouse_candidate.get("reasons", [])},
        )
    if warehouse_candidate.get("warehouse_object_version") != payload.warehouse_object_version:
        raise BusinessError("WAREHOUSE_VERSION_MAP_INVALID", "仓库版本与候选方案不一致", status_code=409)

    order_ids = list(dict.fromkeys(candidate.get("order_ids", [])))
    if len(order_ids) != len(candidate.get("order_ids", [])):
        raise BusinessError("CANDIDATE_ORDER_DUPLICATED", "候选方案包含重复订单", status_code=409)
    orders = list(db.scalars(select(TransportOrder).where(TransportOrder.id.in_(order_ids))))
    if len(orders) != len(order_ids):
        raise BusinessError("CANDIDATE_ORDER_MISSING", "候选方案中的订单已不存在", status_code=409)
    versions = candidate.get("order_versions") or run.input_snapshot.get("order_versions", {})
    for order in orders:
        expected = versions.get(order.id)
        if expected is None or order.object_version != expected:
            raise version_conflict(
                order.object_version,
                expected,
                object_type="transport_orders",
                object_id=order.id,
            )
        if order.status != "DRAFT":
            raise BusinessError("ORDER_STATE_CHANGED", "候选订单状态已经变化，请重新计算", status_code=409)

    warehouse = db.get(Warehouse, payload.warehouse_id)
    if warehouse is None:
        raise BusinessError("WAREHOUSE_NOT_FOUND", "所选仓库不存在", status_code=404)
    if warehouse.object_version != payload.warehouse_object_version:
        raise version_conflict(
            warehouse.object_version,
            payload.warehouse_object_version,
            object_type="warehouses",
            object_id=warehouse.id,
        )
    claimed = db.execute(
        update(AlgorithmRun)
        .where(AlgorithmRun.id == run.id, AlgorithmRun.confirmed.is_(False))
        .values(confirmed=True)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise BusinessError("MATCH_RUN_CONFIRMED", "该拼仓方案已经确认", status_code=409)

    plan = WarehousePoolPlan(
        plan_no=f"WHP-{utcnow().strftime('%y%m%d%H%M%S')}-{run.id[:4].upper()}",
        algorithm_run_id=run.id,
        warehouse_id=warehouse.id,
        order_ids=order_ids,
        required_volume_m3=float(candidate["required_volume_m3"]),
        inbound_start=datetime.fromisoformat(candidate["inbound_window"]["start"]),
        inbound_end=datetime.fromisoformat(candidate["inbound_window"]["end"]),
        outbound_start=datetime.fromisoformat(candidate["outbound_window"]["start"]),
        outbound_end=datetime.fromisoformat(candidate["outbound_window"]["end"]),
        status="RESERVATION_REQUESTED",
    )
    db.add(plan)
    db.flush()
    response_body = {
        "plan": jsonable_encoder(_warehouse_plan_data(db, plan)),
        "status": plan.status,
        "object_version": plan.object_version,
    }
    add_outbox(
        db,
        "warehouse.pool.reservation_requested",
        "warehouse_pool_plan",
        plan.id,
        plan.object_version,
        {
            "plan_id": plan.id,
            "warehouse_id": warehouse.id,
            "warehouse_object_version": payload.warehouse_object_version,
            "actor_user_id": user.id,
            "idempotency_key": idempotency_key,
        },
    )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "CONFIRM_WAREHOUSE_POOL",
        "warehouse_pool_plan",
        plan.id,
        None,
        {"status": plan.status, "warehouse_id": warehouse.id, "required_volume_m3": plan.required_volume_m3},
    )
    save_idempotent(db, scope, idempotency_key, request_body, response_body, 202)
    db.commit()
    return api_payload(request, **response_body)


@app.get(f"{WEB}/warehouse-pool/plans/{{plan_id}}", tags=["拼仓业务"])
def warehouse_pool_plan_detail(
    plan_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    plan = db.get(WarehousePoolPlan, plan_id)
    if plan is None:
        raise BusinessError("WAREHOUSE_PLAN_NOT_FOUND", "拼仓计划不存在", status_code=404)
    return api_payload(request, plan=_warehouse_plan_data(db, plan))


@app.get(f"{WEB}/warehouse-pool/plans", tags=["拼仓业务"])
def list_warehouse_pool_plans(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    total = db.scalar(select(func.count()).select_from(WarehousePoolPlan)) or 0
    plans = list(
        db.scalars(
            select(WarehousePoolPlan)
            .order_by(WarehousePoolPlan.updated_at.desc(), WarehousePoolPlan.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return page_payload(
        request,
        [_warehouse_plan_data(db, plan) for plan in plans],
        page=page,
        page_size=page_size,
        total=total,
    )


def _request_warehouse_plan_action(
    db: Session,
    *,
    plan: WarehousePoolPlan,
    expected_version: int,
    action: str,
    requested_status: str,
    allowed_statuses: tuple[str, ...],
    request_body: dict[str, Any],
    idempotency_key: str,
    trace_id: str,
    user: User,
) -> dict[str, Any]:
    scope = f"warehouse-plan-{action.lower()}:{plan.id}"
    cached = load_idempotent(db, scope, idempotency_key, request_body)
    if cached is not None:
        return cached
    if plan.object_version != expected_version:
        raise version_conflict(
            plan.object_version,
            expected_version,
            object_type="warehouse_pool_plans",
            object_id=plan.id,
        )
    if plan.status not in allowed_statuses:
        raise BusinessError("INVALID_TRANSITION", "当前拼仓计划不能执行该操作", status_code=409)
    before = {"status": plan.status, "object_version": plan.object_version}
    values: dict[str, Any] = {"status": requested_status, "last_error": None}
    if action == "CANCEL":
        values["cancellation_reason"] = request_body["reason"]
    next_version = atomic_versioned_update(
        db,
        WarehousePoolPlan,
        plan.id,
        expected_version,
        values,
        conditions=(WarehousePoolPlan.status.in_(allowed_statuses),),
    )
    response_body = {"plan_id": plan.id, "status": requested_status, "object_version": next_version}
    add_outbox(
        db,
        f"warehouse.pool.{action.lower()}_requested",
        "warehouse_pool_plan",
        plan.id,
        next_version,
        {
            **response_body,
            "actor_user_id": user.id,
            "idempotency_key": idempotency_key,
            "reason": request_body.get("reason"),
        },
    )
    add_audit(
        db,
        trace_id,
        user.id,
        f"{action}_WAREHOUSE_POOL",
        "warehouse_pool_plan",
        plan.id,
        before,
        response_body,
    )
    save_idempotent(db, scope, idempotency_key, request_body, response_body, 202)
    db.commit()
    return response_body


@app.post(f"{WEB}/warehouse-pool/plans/{{plan_id}}/occupy", status_code=202, tags=["拼仓业务"])
def occupy_warehouse_pool_plan(
    plan_id: str,
    payload: VersionedRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    plan = db.get(WarehousePoolPlan, plan_id)
    if plan is None:
        raise BusinessError("WAREHOUSE_PLAN_NOT_FOUND", "拼仓计划不存在", status_code=404)
    result = _request_warehouse_plan_action(
        db,
        plan=plan,
        expected_version=payload.object_version,
        action="OCCUPY",
        requested_status="OCCUPY_REQUESTED",
        allowed_statuses=("RESERVED",),
        request_body=payload.model_dump(),
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
        user=user,
    )
    return api_payload(request, **result)


@app.post(f"{WEB}/warehouse-pool/plans/{{plan_id}}/release", status_code=202, tags=["拼仓业务"])
def release_warehouse_pool_plan(
    plan_id: str,
    payload: VersionedRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    plan = db.get(WarehousePoolPlan, plan_id)
    if plan is None:
        raise BusinessError("WAREHOUSE_PLAN_NOT_FOUND", "拼仓计划不存在", status_code=404)
    result = _request_warehouse_plan_action(
        db,
        plan=plan,
        expected_version=payload.object_version,
        action="RELEASE",
        requested_status="RELEASE_REQUESTED",
        allowed_statuses=("OCCUPIED",),
        request_body=payload.model_dump(),
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
        user=user,
    )
    return api_payload(request, **result)


@app.post(f"{WEB}/warehouse-pool/plans/{{plan_id}}/cancel", status_code=202, tags=["拼仓业务"])
def cancel_warehouse_pool_plan(
    plan_id: str,
    payload: CancelRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    plan = db.get(WarehousePoolPlan, plan_id)
    if plan is None:
        raise BusinessError("WAREHOUSE_PLAN_NOT_FOUND", "拼仓计划不存在", status_code=404)
    result = _request_warehouse_plan_action(
        db,
        plan=plan,
        expected_version=payload.object_version,
        action="CANCEL",
        requested_status="CANCELLATION_REQUESTED",
        allowed_statuses=("RESERVATION_REQUESTED", "RESERVED"),
        request_body=payload.model_dump(),
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
        user=user,
    )
    return api_payload(request, **result)


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
    candidate_ids = list(dict.fromkeys(candidate["order_ids"]))
    if len(candidate_ids) != len(candidate["order_ids"]):
        raise BusinessError("CANDIDATE_ORDER_DUPLICATED", "候选方案包含重复订单", status_code=409)
    orders = list(
        db.scalars(select(TransportOrder).where(TransportOrder.id.in_(candidate_ids)).order_by(TransportOrder.id))
    )
    if len(orders) != len(candidate_ids):
        raise BusinessError("CANDIDATE_ORDER_MISSING", "候选方案中的订单已不存在", status_code=409)

    snapshot_versions = candidate.get("order_versions") or {
        order_id: version
        for order_id, version in run.input_snapshot.get("order_versions", {}).items()
        if order_id in candidate_ids
    }
    if payload.order_versions:
        if set(payload.order_versions) != set(candidate_ids) or (
            snapshot_versions and payload.order_versions != snapshot_versions
        ):
            raise BusinessError("ORDER_VERSION_MAP_INVALID", "订单版本映射与候选方案不一致", status_code=409)
        expected_versions = payload.order_versions
    else:
        saved_versions = set(snapshot_versions.values()) if snapshot_versions else {payload.object_version}
        if len(saved_versions) != 1 or next(iter(saved_versions)) != payload.object_version:
            raise BusinessError("ORDER_VERSIONS_REQUIRED", "候选订单版本不同，请提交逐订单版本", status_code=409)
        expected_versions = {order_id: payload.object_version for order_id in candidate_ids}

    stale = [order.order_no for order in orders if order.object_version != expected_versions[order.id]]
    if stale:
        first = next(order for order in orders if order.object_version != expected_versions[order.id])
        raise version_conflict(
            first.object_version,
            expected_versions[first.id],
            object_type="transport_orders",
            object_id=first.id,
        )
    if any(order.status != "DRAFT" for order in orders):
        raise BusinessError("ORDER_STATE_CHANGED", "候选订单状态已经变化，请重新计算", status_code=409)

    claimed = db.execute(
        update(AlgorithmRun)
        .where(AlgorithmRun.id == run.id, AlgorithmRun.confirmed.is_(False))
        .values(confirmed=True)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise BusinessError("MATCH_RUN_CONFIRMED", "该拼车方案已经确认", status_code=409)
    plan = TransportPlan(
        plan_no=f"PLAN-{utcnow().strftime('%y%m%d%H%M%S')}-{run.id[:4].upper()}",
        algorithm_run_id=run.id,
        candidate_snapshot=candidate,
        status="CONFIRMED",
    )
    db.add(plan)
    db.flush()
    for order in orders:
        atomic_versioned_update(
            db,
            TransportOrder,
            order.id,
            expected_versions[order.id],
            {"status": "CONFIRMED"},
            conditions=(TransportOrder.status == "DRAFT",),
        )
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
    _: User = Depends(require_roles("park_admin", "enterprise_admin")),
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
    payload["data_cutoff"] = cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff
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
    payload["data_cutoff"] = cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff
    return payload


@app.get(f"{WEB}/telemetry-issues", tags=["遥测一致性投影"])
def projected_telemetry_issues(
    request: Request,
    status: str | None = None,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    filters = _operation_filters(TelemetryIssueProjection, user, db, status=status, period=period)
    payload = _operation_page(
        request,
        db,
        TelemetryIssueProjection,
        [
            "id",
            "source_issue_id",
            "task_id",
            "store_id",
            "issue_type",
            "source_type",
            "severity",
            "status",
            "task_status",
            "expected_vehicle_id",
            "actual_vehicle_id",
            "expected_driver_id",
            "actual_driver_id",
            "message",
            "business_at",
        ],
        filters,
        page=page,
        page_size=page_size,
        unit="条",
        period=period,
        label_mapping=("issue_type", TELEMETRY_ISSUE_TYPE_LABELS),
    )
    for item in payload["items"]:
        item["status_label"] = enum_label(TELEMETRY_ISSUE_STATUS_LABELS, item["status"])
    return payload


def _operation_filters(
    model: type[Any],
    user: User,
    db: Session,
    *,
    store_id: str | None = None,
    product_id: str | None = None,
    status: str | None = None,
    period: str = "30d",
) -> list[Any]:
    filters: list[Any] = []
    window = period_window(period)
    allowed_store_ids = scoped_store_ids(db, user)
    if allowed_store_ids is not None:
        filters.append(model.store_id.in_(allowed_store_ids))
    if store_id:
        filters.append(model.store_id == store_id)
    if product_id and hasattr(model, "product_id"):
        filters.append(model.product_id == product_id)
    if status:
        status_column = getattr(model, "receipt_status", None)
        if status_column is None:
            status_column = getattr(model, "status", None)
        if status_column is not None:
            filters.append(status_column == status)
    if model is StoreDailyReportProjection:
        filters.extend(
            (
                StoreDailyReportProjection.report_date >= window.start_date,
                StoreDailyReportProjection.report_date <= window.end_date,
            )
        )
    else:
        filters.extend((model.business_at >= window.start_at, model.business_at < window.end_at))
    return filters


def _operation_page(
    request: Request,
    db: Session,
    model: type[Any],
    fields: list[str],
    filters: list[Any],
    *,
    page: int,
    page_size: int,
    unit: str,
    period: str,
    label_mapping: tuple[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(model).where(*filters)) or 0
    rows = list(
        db.scalars(
            select(model)
            .where(*filters)
            .order_by(model.business_at.desc(), model.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    store_ids = {row.store_id for row in rows}
    product_ids = {row.product_id for row in rows if hasattr(row, "product_id")}
    stores = {item.id: item for item in db.scalars(select(Store).where(Store.id.in_(store_ids)))} if store_ids else {}
    products = (
        {item.id: item for item in db.scalars(select(Product).where(Product.id.in_(product_ids)))}
        if product_ids
        else {}
    )
    items = []
    for row in rows:
        item = _model_item(row, fields)
        store = stores.get(row.store_id)
        item["store_name"] = store.name if store else "未知门店"
        item["store_channel"] = store.channel if store else "UNKNOWN"
        if hasattr(row, "product_id"):
            product = products.get(row.product_id)
            item["product_name"] = product.name if product else "未知商品"
            item["product_unit"] = product.unit if product else "未知单位"
        if label_mapping:
            field, mapping = label_mapping
            item[f"{field}_label"] = enum_label(mapping, getattr(row, field))
        items.append(item)
    cutoff = db.scalar(select(func.max(model.business_at)).where(*filters))
    payload = page_payload(request, items, page=page, page_size=page_size, total=total)
    payload.update(period=period, unit=unit, data_cutoff=cutoff.isoformat() if cutoff else None)
    return payload


@app.get(f"{WEB}/receipts", tags=["经营数据投影"])
def projected_receipts(
    request: Request,
    store_id: str | None = None,
    status: str | None = None,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    filters = _operation_filters(ReceiptProjection, user, db, store_id=store_id, status=status, period=period)
    return _operation_page(
        request,
        db,
        ReceiptProjection,
        [
            "id",
            "source_receipt_id",
            "task_id",
            "store_id",
            "receipt_status",
            "expected_total",
            "received_total",
            "difference_total",
            "rejected_line_count",
            "details",
            "source_version",
            "business_at",
        ],
        filters,
        page=page,
        page_size=page_size,
        unit="单/商品基础单位",
        period=period,
        label_mapping=("receipt_status", RECEIPT_STATUS_LABELS),
    )


@app.get(f"{WEB}/inventory-movements", tags=["经营数据投影"])
def projected_inventory_movements(
    request: Request,
    store_id: str | None = None,
    product_id: str | None = None,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    filters = _operation_filters(
        InventoryMovementProjection,
        user,
        db,
        store_id=store_id,
        product_id=product_id,
        period=period,
    )
    payload = _operation_page(
        request,
        db,
        InventoryMovementProjection,
        [
            "id",
            "source_movement_id",
            "store_id",
            "product_id",
            "movement_type",
            "quantity_before",
            "quantity_delta",
            "quantity_after",
            "quantity_context",
            "source_type",
            "source_id",
            "business_at",
        ],
        filters,
        page=page,
        page_size=page_size,
        unit="商品基础单位",
        period=period,
        label_mapping=("movement_type", MOVEMENT_TYPE_LABELS),
    )
    for item in payload["items"]:
        item["quantity_context_label"] = enum_label(
            INVENTORY_QUANTITY_CONTEXT_LABELS,
            item["quantity_context"],
        )
    return payload


@app.get(f"{WEB}/stockout-demands", tags=["经营数据投影"])
def projected_stockout_demands(
    request: Request,
    store_id: str | None = None,
    product_id: str | None = None,
    status: str | None = None,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    filters = _operation_filters(
        StockoutDemandProjection,
        user,
        db,
        store_id=store_id,
        product_id=product_id,
        status=status,
        period=period,
    )
    return _operation_page(
        request,
        db,
        StockoutDemandProjection,
        [
            "id",
            "source_stockout_id",
            "store_id",
            "product_id",
            "requested_quantity",
            "reason",
            "status",
            "source_version",
            "business_at",
        ],
        filters,
        page=page,
        page_size=page_size,
        unit="商品基础单位",
        period=period,
        label_mapping=("status", STOCKOUT_STATUS_LABELS),
    )


@app.get(f"{WEB}/store-daily-reports", tags=["经营数据投影"])
def projected_store_daily_reports(
    request: Request,
    store_id: str | None = None,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    filters = _operation_filters(StoreDailyReportProjection, user, db, store_id=store_id, period=period)
    return _operation_page(
        request,
        db,
        StoreDailyReportProjection,
        [
            "id",
            "source_report_id",
            "store_id",
            "report_date",
            "sales_amount",
            "order_count",
            "authorized_for_dashboard",
            "summary",
            "source_version",
            "business_at",
        ],
        filters,
        page=page,
        page_size=page_size,
        unit="人民币/单",
        period=period,
    )


@app.get(f"{WEB}/operations/summary", tags=["经营数据投影"])
def projected_operations_summary(
    request: Request,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    summary = operations_summary(db, user, period)
    summary.update(currency="CNY", sales_amount_unit="yuan", order_count_unit="单")
    cutoff = summary.pop("data_cutoff")
    payload = api_payload(request, data_cutoff=cutoff, **summary)
    payload.setdefault("data_cutoff", None)
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
        raise version_conflict(plan.object_version, payload.object_version)
    if plan.status != "READY" or not plan.task_id:
        raise BusinessError("PLAN_NOT_READY", "执行任务尚未建立，请稍后刷新", status_code=409)
    before = {"status": plan.status, "object_version": plan.object_version, "task_id": plan.task_id}
    next_plan_version = atomic_versioned_update(
        db,
        TransportPlan,
        plan.id,
        payload.object_version,
        {"status": "PUBLISH_REQUESTED"},
        conditions=(TransportPlan.status == "READY", TransportPlan.task_id.is_not(None)),
    )
    response_body = {
        "plan_id": plan.id,
        "task_id": plan.task_id,
        "status": "PUBLISH_REQUESTED",
        "object_version": next_plan_version,
    }
    add_outbox(
        db,
        "transport.plan.publish_requested",
        "transport_plan",
        plan.id,
        next_plan_version,
        {**response_body, "actor_user_id": user.id, "idempotency_key": idempotency_key},
    )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "PUBLISH_PLAN",
        "transport_plan",
        plan.id,
        before,
        response_body,
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
        raise version_conflict(plan.object_version, payload.object_version)
    if plan.status not in {"CONFIRMED", "READY", "PUBLISHED"}:
        raise BusinessError("INVALID_TRANSITION", "当前运输计划不能申请取消", status_code=409)
    before = {
        "status": plan.status,
        "object_version": plan.object_version,
        "task_id": plan.task_id,
        "cancellation_reason": plan.cancellation_reason,
    }
    next_plan_version = atomic_versioned_update(
        db,
        TransportPlan,
        plan.id,
        payload.object_version,
        {
            "status": "CANCELLATION_REQUESTED",
            "cancellation_reason": payload.reason,
            "last_error": None,
        },
        conditions=(TransportPlan.status.in_(("CONFIRMED", "READY", "PUBLISHED")),),
    )
    response_body = {
        "plan_id": plan.id,
        "task_id": plan.task_id,
        "status": "CANCELLATION_REQUESTED",
        "object_version": next_plan_version,
    }
    add_outbox(
        db,
        "transport.plan.cancellation_requested",
        "transport_plan",
        plan.id,
        next_plan_version,
        {
            **response_body,
            "reason": payload.reason,
            "actor_user_id": user.id,
            "idempotency_key": idempotency_key,
        },
    )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "CANCEL_PLAN",
        "transport_plan",
        plan.id,
        before,
        {**response_body, "reason": payload.reason},
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
    return api_payload(
        request,
        run_id=run.id,
        algorithm_type="PROCUREMENT",
        algorithm_type_label=enum_label(ALGORITHM_TYPE_LABELS, "PROCUREMENT"),
        **result,
    )


@app.post(f"{WEB}/procurement/demands", status_code=201, tags=["采购自动汇总"])
def confirm_procurement_demand(
    payload: ProcurementDemandConfirmRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin", "enterprise_admin")),
) -> dict:
    cycle_start, cycle_end = procurement_cycle(payload.cycle_start)
    if user.role == "enterprise_admin" and user.enterprise_id != payload.enterprise_id:
        raise BusinessError("FORBIDDEN", "只能确认本企业的采购需求", status_code=403)
    if db.get(Enterprise, payload.enterprise_id) is None:
        raise BusinessError("ENTERPRISE_NOT_FOUND", "企业不存在", status_code=404)
    if db.get(Product, payload.product_id) is None:
        raise BusinessError("PRODUCT_NOT_FOUND", "商品不存在", status_code=404)
    request_body = {**payload.model_dump(mode="json"), "cycle_start": cycle_start.isoformat()}
    scope = f"procurement-demand:{payload.enterprise_id}:{payload.product_id}:{cycle_start.isoformat()}"
    cached = load_idempotent(db, scope, idempotency_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    existing = db.scalar(
        select(ProcurementDemandConfirmation).where(
            ProcurementDemandConfirmation.enterprise_id == payload.enterprise_id,
            ProcurementDemandConfirmation.product_id == payload.product_id,
            ProcurementDemandConfirmation.cycle_start == cycle_start,
        )
    )
    now = utcnow()
    if existing is None:
        if payload.object_version is not None:
            raise BusinessError("DEMAND_VERSION_NOT_APPLICABLE", "首次确认需求时不能提供对象版本", status_code=409)
        item = ProcurementDemandConfirmation(
            enterprise_id=payload.enterprise_id,
            product_id=payload.product_id,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            quantity=payload.quantity,
            unit=payload.unit,
            status="CONFIRMED",
            reason=payload.reason,
            confirmed_by=user.id,
            confirmed_at=now,
        )
        db.add(item)
        db.flush()
        next_version = item.object_version
        before = None
    else:
        if payload.object_version is None:
            raise BusinessError("DEMAND_OBJECT_VERSION_REQUIRED", "更新已确认需求必须提供对象版本", status_code=409)
        before = {
            "quantity": existing.quantity,
            "unit": existing.unit,
            "reason": existing.reason,
            "object_version": existing.object_version,
        }
        next_version = atomic_versioned_update(
            db,
            ProcurementDemandConfirmation,
            existing.id,
            payload.object_version,
            {
                "quantity": payload.quantity,
                "unit": payload.unit,
                "reason": payload.reason,
                "confirmed_by": user.id,
                "confirmed_at": now,
                "status": "CONFIRMED",
            },
            conditions=(ProcurementDemandConfirmation.status == "CONFIRMED",),
        )
        item = existing
    response_body = {
        "id": item.id,
        "enterprise_id": payload.enterprise_id,
        "product_id": payload.product_id,
        "cycle_start": cycle_start.isoformat(),
        "cycle_end": cycle_end.isoformat(),
        "quantity": payload.quantity,
        "unit": payload.unit,
        "status": "CONFIRMED",
        "object_version": next_version,
    }
    add_outbox(
        db,
        "procurement.demand.confirmed",
        "procurement_demand_confirmation",
        item.id,
        next_version,
        response_body,
    )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "CONFIRM_PROCUREMENT_DEMAND",
        "procurement_demand_confirmation",
        item.id,
        before,
        response_body,
    )
    save_idempotent(db, scope, idempotency_key, request_body, response_body, 201)
    db.commit()
    return api_payload(request, **response_body)


@app.post(f"{WEB}/procurement/aggregations/generate", status_code=201, tags=["采购自动汇总"])
def generate_procurement_board(
    payload: ProcurementGenerateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    requested_cycle, _ = procurement_cycle(payload.cycle_start)
    request_body = {"cycle_start": requested_cycle.isoformat()}
    scope = f"procurement-aggregation:{requested_cycle.isoformat()}"
    cached = load_idempotent(db, scope, idempotency_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    cycle_start, cycle_end, aggregations = generate_procurement_aggregations(db, requested_cycle)
    items = [aggregation_data(item, db.get(Product, item.product_id)) for item in aggregations]
    response_body = {
        "cycle_start": cycle_start.isoformat(),
        "cycle_end": cycle_end.isoformat(),
        "rules_version": "procurement-aggregation-v1",
        "unit": "按商品基础单位",
        "items": items,
        "data_cutoff": max(
            (item["data_cutoff"] for item in items if item["data_cutoff"]),
            default=None,
        ),
    }
    for aggregation in aggregations:
        add_outbox(
            db,
            "procurement.aggregation.generated",
            "procurement_aggregation",
            aggregation.id,
            aggregation.object_version,
            {
                "aggregation_id": aggregation.id,
                "product_id": aggregation.product_id,
                "cycle_start": cycle_start.isoformat(),
                "automatic_quantity": aggregation.automatic_quantity,
            },
        )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "GENERATE_PROCUREMENT_AGGREGATION",
        "procurement_cycle",
        aggregations[0].id if aggregations else user.id,
        None,
        {"cycle_start": cycle_start.isoformat(), "item_count": len(items)},
    )
    save_idempotent(db, scope, idempotency_key, request_body, response_body, 201)
    db.commit()
    return api_payload(request, **response_body)


@app.get(f"{WEB}/procurement/aggregations", tags=["采购自动汇总"])
def list_procurement_aggregations(
    request: Request,
    cycle_start: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin")),
) -> dict:
    filters = [ProcurementAggregation.cycle_start == cycle_start] if cycle_start else []
    total = db.scalar(select(func.count()).select_from(ProcurementAggregation).where(*filters)) or 0
    rows = list(
        db.scalars(
            select(ProcurementAggregation)
            .where(*filters)
            .order_by(ProcurementAggregation.cycle_start.desc(), ProcurementAggregation.product_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return page_payload(
        request,
        [aggregation_data(row, db.get(Product, row.product_id)) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@app.post(f"{WEB}/procurement/aggregations/{{aggregation_id}}/confirm", tags=["采购自动汇总"])
def confirm_procurement_aggregation(
    aggregation_id: str,
    payload: ProcurementAggregationConfirmRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    aggregation = db.get(ProcurementAggregation, aggregation_id)
    if aggregation is None:
        raise BusinessError("PROCUREMENT_AGGREGATION_NOT_FOUND", "采购汇总不存在", status_code=404)
    request_body = payload.model_dump()
    scope = f"confirm-procurement-aggregation:{aggregation_id}"
    cached = load_idempotent(db, scope, idempotency_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    if aggregation.object_version != payload.object_version:
        raise version_conflict(
            aggregation.object_version,
            payload.object_version,
            object_type="procurement_aggregations",
            object_id=aggregation.id,
        )
    if aggregation.status != "DRAFT":
        raise BusinessError("INVALID_TRANSITION", "当前采购汇总已经确认", status_code=409)
    confirmed_quantity = (
        payload.adjusted_quantity if payload.adjusted_quantity is not None else aggregation.automatic_quantity
    )
    now = utcnow()
    next_version = atomic_versioned_update(
        db,
        ProcurementAggregation,
        aggregation.id,
        payload.object_version,
        {
            "status": "CONFIRMED",
            "adjusted_quantity": confirmed_quantity,
            "adjustment_reason": payload.adjustment_reason,
            "confirmed_by": user.id,
            "confirmed_at": now,
        },
        conditions=(ProcurementAggregation.status == "DRAFT",),
    )
    response_body = {
        "aggregation_id": aggregation.id,
        "status": "CONFIRMED",
        "automatic_quantity": aggregation.automatic_quantity,
        "confirmed_quantity": confirmed_quantity,
        "unit": aggregation.base_unit,
        "object_version": next_version,
    }
    add_outbox(
        db,
        "procurement.aggregation.confirmed",
        "procurement_aggregation",
        aggregation.id,
        next_version,
        response_body,
    )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "CONFIRM_PROCUREMENT_AGGREGATION",
        "procurement_aggregation",
        aggregation.id,
        {"status": aggregation.status, "object_version": aggregation.object_version},
        response_body,
    )
    save_idempotent(db, scope, idempotency_key, request_body, response_body)
    db.commit()
    return api_payload(request, **response_body)


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
    result["method_label"] = enum_label(FORECAST_METHOD_LABELS, result["method"])
    return api_payload(
        request,
        run_id=run.id,
        algorithm_type="DEMAND_FORECAST",
        algorithm_type_label=enum_label(ALGORITHM_TYPE_LABELS, "DEMAND_FORECAST"),
        data_cutoff=result["data_cutoff"],
        forecast=result,
    )


@app.post(f"{WEB}/forecasts/next-week/generate", status_code=201, tags=["下周需求预测"])
def generate_next_week_forecast_batch(
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("park_admin")),
) -> dict:
    forecast_start, forecast_end = next_week_window()
    request_body = {"forecast_start": forecast_start.isoformat(), "forecast_end": forecast_end.isoformat()}
    scope = f"next-week-forecast:{forecast_start.isoformat()}"
    cached = load_idempotent(db, scope, idempotency_key, request_body)
    if cached is not None:
        return api_payload(request, **cached)
    batch, projections, changed = generate_next_week_forecasts(db, scheduled=False)
    items = [
        forecast_projection_data(
            item,
            db.get(Enterprise, item.enterprise_id),
            db.get(Product, item.product_id),
        )
        for item in projections
    ]
    response_body = {
        "batch": {
            "id": batch.id,
            "forecast_start": batch.forecast_start.isoformat(),
            "forecast_end": batch.forecast_end.isoformat(),
            "rules_version": batch.rules_version,
            "status": batch.status,
            "item_count": batch.item_count,
            "object_version": batch.object_version,
            "scheduled": batch.scheduled,
        },
        "period": {"start": forecast_start.isoformat(), "end": forecast_end.isoformat()},
        "unit": "按商品基础单位",
        "items": items,
        "aggregates": batch.aggregate_snapshot,
        "data_cutoff": as_utc(batch.data_cutoff).isoformat() if batch.data_cutoff else None,
        "changed_count": changed,
    }
    if changed:
        add_outbox(
            db,
            "forecast.next_week.generated",
            "demand_forecast_batch",
            batch.id,
            batch.object_version,
            {
                "batch_id": batch.id,
                "forecast_start": batch.forecast_start.isoformat(),
                "forecast_end": batch.forecast_end.isoformat(),
                "item_count": len(items),
                "changed_count": changed,
            },
        )
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "GENERATE_NEXT_WEEK_FORECAST",
        "demand_forecast_batch",
        batch.id,
        None,
        {"forecast_start": forecast_start.isoformat(), "item_count": len(items), "changed_count": changed},
    )
    save_idempotent(db, scope, idempotency_key, request_body, response_body, 201)
    db.commit()
    return api_payload(request, **response_body)


@app.get(f"{WEB}/forecasts/next-week", tags=["下周需求预测"])
def next_week_forecast_batch(
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("park_admin")),
) -> dict:
    forecast_start, forecast_end = next_week_window()
    batch = db.scalar(select(DemandForecastBatch).where(DemandForecastBatch.forecast_start == forecast_start))
    if batch is None:
        raise BusinessError("FORECAST_BATCH_NOT_READY", "下周预测批次尚未生成", status_code=404)
    projections = list(
        db.scalars(
            select(DemandForecastProjection)
            .where(DemandForecastProjection.forecast_start == forecast_start)
            .order_by(DemandForecastProjection.product_id, DemandForecastProjection.enterprise_id)
        )
    )
    items = [
        forecast_projection_data(
            item,
            db.get(Enterprise, item.enterprise_id),
            db.get(Product, item.product_id),
        )
        for item in projections
    ]
    return api_payload(
        request,
        data_cutoff=as_utc(batch.data_cutoff) if batch.data_cutoff else None,
        batch={
            "id": batch.id,
            "forecast_start": batch.forecast_start.isoformat(),
            "forecast_end": batch.forecast_end.isoformat(),
            "rules_version": batch.rules_version,
            "status": batch.status,
            "item_count": batch.item_count,
            "object_version": batch.object_version,
            "scheduled": batch.scheduled,
        },
        period={"start": forecast_start.isoformat(), "end": forecast_end.isoformat()},
        unit="按商品基础单位",
        items=items,
        aggregates=batch.aggregate_snapshot,
    )


def _dashboard_payload(request: Request, snapshot: dict[str, Any]) -> dict[str, Any]:
    cutoff = snapshot.pop("data_cutoff")
    payload = api_payload(request, data_cutoff=cutoff, **snapshot)
    payload.setdefault("data_cutoff", None)
    if cutoff is None:
        payload["data_cutoff_note"] = "暂无可用于确定业务数据截止时间的记录"
    return payload


@app.get(f"{WEB}/dashboard/snapshot", tags=["网页看板"])
def web_snapshot(
    request: Request,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    snapshot = (
        refresh_dashboard_projection(db, period)
        if not get_settings().is_production
        else read_dashboard_projection(db, period)
    )
    if snapshot is None:
        raise BusinessError("DASHBOARD_NOT_READY", "看板投影尚未生成", status_code=503)
    return _dashboard_payload(request, snapshot)


@app.get("/api/v1/public/dashboard/snapshot", tags=["公开大屏"])
def public_snapshot(
    request: Request,
    period: str = Query("30d", pattern="^(7d|30d|month)$"),
    db: Session = Depends(get_db),
) -> dict:
    snapshot = (
        refresh_dashboard_projection(db, period)
        if not get_settings().is_production
        else read_dashboard_projection(db, period)
    )
    if snapshot is None:
        raise BusinessError("DASHBOARD_NOT_READY", "看板投影尚未生成", status_code=503)
    return _dashboard_payload(request, snapshot)


def _information_response(
    request: Request,
    *,
    kind: str,
    limit: int,
    legacy: bool = False,
) -> Response | dict:
    if not get_settings().public_information_enabled:
        raise BusinessError(
            "PUBLIC_INFORMATION_UNAVAILABLE",
            "园区资讯功能正在更新",
            status_code=503,
        )
    try:
        result = public_information(kind, limit)
    except (OSError, ValueError) as exc:
        logger.warning("public information catalog unavailable: %s", type(exc).__name__)
        raise BusinessError(
            "PUBLIC_INFORMATION_UNAVAILABLE",
            "园区资讯暂不可用",
            status_code=503,
        ) from exc
    etag = result.pop("etag")
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=60, stale-while-revalidate=300",
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)
    cutoff = result.pop("data_cutoff")
    if legacy:
        result = {"data": result.pop("items"), **result}
    payload = api_payload(request, data_cutoff=cutoff, **result)
    return Response(
        content=json.dumps(jsonable_encoder(payload), ensure_ascii=False),
        media_type="application/json",
        headers=headers,
    )


@app.get("/api/v1/public/dashboard/information", tags=["公开大屏"], response_model=None)
def dashboard_information(
    request: Request,
    kind: str = Query("all", pattern="^(all|news|policy)$"),
    limit: int = Query(8, ge=1, le=20),
) -> Response | dict:
    return _information_response(request, kind=kind, limit=limit)


@app.get("/api/v1/public/dashboard/news", tags=["公开大屏"], response_model=None)
def dashboard_news(request: Request, limit: int = Query(6, ge=1, le=20)) -> Response | dict:
    return _information_response(request, kind="news", limit=limit, legacy=True)


@app.get("/api/v1/public/dashboard/policies", tags=["公开大屏"], response_model=None)
def dashboard_policies(request: Request, limit: int = Query(6, ge=1, le=20)) -> Response | dict:
    return _information_response(request, kind="policy", limit=limit, legacy=True)


@app.post(f"{WEB}/assistant/query", tags=["数据助手"])
def assistant_query(
    payload: AssistantQuery,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    if not get_settings().is_production:
        refresh_dashboard_projection(db, payload.period)
    assistant_started = time.perf_counter()
    try:
        result = answer_data_question(
            db,
            payload.question,
            payload.preferred_chart,
            payload.period,
            allow_external_classifier=True,
        )
    except BusinessError as exc:
        assistant_latency_ms = round((time.perf_counter() - assistant_started) * 1_000, 2)
        try:
            add_audit(
                db,
                request.state.trace_id,
                user.id,
                "ASSISTANT_QUERY",
                "assistant_query",
                request.state.trace_id,
                None,
                {
                    "period": payload.period,
                    "status": "FAILED",
                    "error_code": exc.code,
                    "assistant_latency_ms": assistant_latency_ms,
                    "data_cutoff": None,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
        raise
    cutoff = result.pop("data_cutoff")
    assistant_latency_ms = round((time.perf_counter() - assistant_started) * 1_000, 2)
    add_audit(
        db,
        request.state.trace_id,
        user.id,
        "ASSISTANT_QUERY",
        "assistant_query",
        request.state.trace_id,
        None,
        {
            "intent": result["intent"],
            "period": payload.period,
            "mode": result["mode"],
            "status": "COMPLETED",
            "assistant_latency_ms": assistant_latency_ms,
            "data_cutoff": cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff,
        },
    )
    db.commit()
    response = api_payload(request, data_cutoff=cutoff, **result)
    if cutoff is None:
        response["data_cutoff"] = None
        response["data_cutoff_note"] = "该查询没有可核验的业务数据截止时间"
    return response


@app.post("/api/v1/public/assistant/query", tags=["公开数据助手"])
def public_assistant_query(payload: AssistantQuery, request: Request, db: Session = Depends(get_db)) -> dict:
    subject_hash = None
    if get_settings().assistant_public_db_quota_enabled:
        subject_hash = anonymous_subject_hash(request)
        claim_public_text_quota(db, subject_hash)
    if not get_settings().is_production:
        refresh_dashboard_projection(db, payload.period)
    assistant_started = time.perf_counter()
    try:
        result = answer_data_question(
            db,
            payload.question,
            payload.preferred_chart,
            payload.period,
            allow_external_classifier=False,
        )
    except BusinessError as exc:
        assistant_latency_ms = round((time.perf_counter() - assistant_started) * 1_000, 2)
        try:
            add_audit(
                db,
                request.state.trace_id,
                None,
                "PUBLIC_ASSISTANT_QUERY",
                "assistant_query",
                request.state.trace_id,
                None,
                {
                    "subject_hash": subject_hash,
                    "period": payload.period,
                    "status": "FAILED",
                    "error_code": exc.code,
                    "assistant_latency_ms": assistant_latency_ms,
                    "data_cutoff": None,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
        raise
    cutoff = result.pop("data_cutoff")
    assistant_latency_ms = round((time.perf_counter() - assistant_started) * 1_000, 2)
    add_audit(
        db,
        request.state.trace_id,
        None,
        "PUBLIC_ASSISTANT_QUERY",
        "assistant_query",
        request.state.trace_id,
        None,
        {
            "subject_hash": subject_hash,
            "intent": result["intent"],
            "period": payload.period,
            "mode": result["mode"],
            "status": "COMPLETED",
            "assistant_latency_ms": assistant_latency_ms,
            "data_cutoff": cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff,
        },
    )
    db.commit()
    response = api_payload(request, data_cutoff=cutoff, **result)
    if cutoff is None:
        response["data_cutoff"] = None
        response["data_cutoff_note"] = "该查询没有可核验的业务数据截止时间"
    return response


@app.post(f"{WEB}/assistant/transcriptions", tags=["数据助手"])
async def assistant_transcription(
    request: Request,
    audio: UploadFile = File(...),
    duration_seconds: float | None = Form(default=None, gt=0, le=30),
    client_request_id: str | None = Form(default=None),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    if bool(getattr(audio.file, "_rolled", False)):
        raise BusinessError("AUDIO_STORAGE_POLICY", "录音上传未满足内存处理策略", status_code=413)
    maximum = get_settings().voice_max_bytes
    content = await audio.read(maximum + 1)
    request_id = client_request_id or idempotency_key or str(uuid4())
    result = await transcribe_voice_request(
        db,
        request,
        audio_bytes=content,
        content_type=audio.content_type or "",
        claimed_duration_seconds=duration_seconds,
        client_request_id=request_id,
        idempotency_key=idempotency_key or request_id,
        public=False,
        actor_user_id=user.id,
    )
    return api_payload(request, **result)


@app.post("/api/v1/public/assistant/transcriptions", tags=["公开数据助手"])
async def public_assistant_transcription(
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    audio: UploadFile = File(...),
    duration_seconds: float = Form(..., gt=0, le=30),
    client_request_id: str = Form(...),
    db: Session = Depends(get_db),
) -> dict:
    if bool(getattr(audio.file, "_rolled", False)):
        raise BusinessError("AUDIO_STORAGE_POLICY", "录音上传未满足内存处理策略", status_code=413)
    maximum = get_settings().voice_max_bytes
    content = await audio.read(maximum + 1)
    result = await transcribe_voice_request(
        db,
        request,
        audio_bytes=content,
        content_type=audio.content_type or "",
        claimed_duration_seconds=duration_seconds,
        client_request_id=client_request_id,
        idempotency_key=idempotency_key,
        public=True,
    )
    return api_payload(request, **result)


@app.get(f"{WEB}/dictionaries", tags=["公共约定"])
def dictionaries(request: Request, _: User = Depends(get_current_user)) -> dict:
    return api_payload(request, dictionaries=dictionary_payload())


@app.get("/api/v1/public/dictionaries", tags=["公共约定"])
def public_dictionaries(request: Request) -> dict:
    return api_payload(request, dictionaries=dictionary_payload())


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
                        .where(
                            OutboxEvent.sequence > cursor,
                            OutboxEvent.topic == "dashboard.snapshot.updated",
                            OutboxEvent.status == "PUBLISHED",
                        )
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
                        "targets": event.payload.get("targets", []),
                        "kinds": event.payload.get("kinds", []),
                        "periods": event.payload.get("periods", []),
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
