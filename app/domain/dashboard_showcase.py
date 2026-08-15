from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.dashboard_catalogs import ShowcaseScenario, catalog_sha256, load_showcase_catalog
from app.domain.forecasts import next_week_window
from app.domain.procurement import procurement_cycle
from app.shared.dictionaries import (
    FORECAST_METHOD_LABELS,
    TEMPERATURE_ZONE_LABELS,
    enum_label,
)
from app.shared.models import (
    AlgorithmRun,
    DemandForecastBatch,
    DemandForecastProjection,
    ProcurementAggregation,
    Product,
)
from app.shared.security import as_utc

UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
FORBIDDEN_KEYS = {
    "id",
    "object_version",
    "input_snapshot",
    "output_snapshot",
    "rules_version",
    "confirmed_by",
    "trace_id",
    "scenario_code",
}


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnmatchedReason(PublicModel):
    reason: str = Field(min_length=1, max_length=240)
    count: int = Field(ge=1)


class InputSummary(PublicModel):
    service_area: str | None = None
    enterprise_count: int = Field(ge=0)
    order_count: int = Field(ge=0)
    product_count: int = Field(ge=0)
    temperature_zones: list[str]
    period_start: str | None = None
    period_end: str | None = None
    window_start: str | None = None
    window_end: str | None = None


class ResultSummary(PublicModel):
    allocation_count: int = Field(ge=0)
    allocated_order_count: int | None = Field(default=None, ge=0)
    unmatched_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)


class CarpoolAllocation(PublicModel):
    type: Literal["CARPOOL"] = "CARPOOL"
    vehicle_label: str
    temperature_zone_label: str
    enterprise_count: int
    order_count: int
    product_count: int
    store_names: list[str]
    departure_start: str
    departure_end: str
    total_weight_kg: float
    total_volume_m3: float
    weight_utilization_pct: float
    volume_utilization_pct: float
    capacity_utilization_pct: float
    merged_route_estimated_km: float
    independent_route_estimated_km: float
    detour_estimated_km: float
    mileage_benefit_estimated_km: float
    route_label: str
    decision_state: str
    decision_state_label: str
    explanation: str


class WarehouseAllocation(PublicModel):
    type: Literal["WAREHOUSE"] = "WAREHOUSE"
    warehouse_name: str
    temperature_zone_label: str
    enterprise_count: int
    order_count: int
    product_count: int
    required_volume_m3: float
    available_volume_m3: float
    remaining_volume_m3: float
    estimated_distance_km: float
    inbound_start: str
    inbound_end: str
    outbound_start: str
    outbound_end: str
    eligible: bool
    decision_state: str
    decision_state_label: str
    reasons: list[str]
    route_label: str


class SupplierOption(PublicModel):
    supplier_name: str
    quantity: float
    unit_price: float
    total_amount: float
    price_score: float
    delivery_score: float
    quality_score: float
    composite_score: float
    tier_min_quantity: float
    tier_max_quantity: float | None
    supply_capacity: float
    quote_valid_from: str
    quote_valid_to: str
    is_recommended: bool
    recommendation_reason: str


class ProcurementAllocation(PublicModel):
    type: Literal["PROCUREMENT"] = "PROCUREMENT"
    product_name: str
    base_unit: str
    cycle_start: str
    cycle_end: str
    automatic_quantity: float
    effective_quantity: float
    quantity_source: str
    quantity_source_label: str
    confirmation_state: str
    confirmation_state_label: str
    supplier_options: list[SupplierOption]
    unit_conversion_warnings: list[dict[str, Any]]


class ForecastAllocation(PublicModel):
    type: Literal["FORECAST"] = "FORECAST"
    product_name: str
    unit: str
    forecast_start: str
    forecast_end: str
    enterprise_count: int
    available_forecast_count: int
    historical_usage: float
    production_plan_quantity: float
    forecast_quantity: float | None
    suggested_purchase_quantity: float
    lower_bound: float | None
    upper_bound: float | None
    mae_average: float | None
    smape_average: float | None
    method_labels: list[str]
    data_insufficient_count: int
    method_note: str


Allocation = CarpoolAllocation | WarehouseAllocation | ProcurementAllocation | ForecastAllocation
ALLOCATION_ADAPTER = TypeAdapter(Allocation)


class ShowcaseCase(PublicModel):
    key: str
    title: str
    description: str
    source_mode: str
    status: str
    status_label: str
    headline: str
    calculated_at: str | None
    data_cutoff: str | None
    data_cutoff_note: str | None
    input_summary: InputSummary
    result_summary: ResultSummary
    allocations: list[dict[str, Any]] = Field(max_length=24)
    unmatched_reasons: list[UnmatchedReason] = Field(max_length=8)


def assert_public_showcase_safe(value: Any, path: str = "algorithm_showcase") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS or key.endswith("_id") or key.endswith("_versions"):
                raise ValueError(f"{path} 包含禁止字段 {key}")
            assert_public_showcase_safe(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_public_showcase_safe(item, f"{path}[{index}]")
    elif isinstance(value, str):
        if UUID_PATTERN.search(value) or "SCENARIO_" in value:
            raise ValueError(f"{path} 包含内部标识")


def _scenario_summary(scenario: ShowcaseScenario) -> InputSummary:
    departures = sorted(order.departure_at for order in scenario.orders)
    return InputSummary(
        service_area=scenario.service_area,
        enterprise_count=len({item.enterprise_name for item in scenario.orders}),
        order_count=len(scenario.orders),
        product_count=len({item.product_name for item in scenario.orders}),
        temperature_zones=[
            enum_label(TEMPERATURE_ZONE_LABELS, value)
            for value in ("AMBIENT", "CHILLED", "FROZEN")
            if any(item.temperature_zone == value for item in scenario.orders)
        ],
        window_start=departures[0].isoformat(),
        window_end=departures[-1].isoformat(),
    )


def _reason_rows(items: list[dict[str, Any]]) -> list[UnmatchedReason]:
    reasons = Counter(str(item.get("reason") or "未形成匹配方案")[:240] for item in items)
    return [
        UnmatchedReason(reason=reason, count=count)
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]


def _latest_run(db: Session, algorithm_type: str, key: str) -> AlgorithmRun | None:
    return db.scalar(
        select(AlgorithmRun)
        .where(
            AlgorithmRun.algorithm_type == algorithm_type,
            AlgorithmRun.showcase_key == key,
            AlgorithmRun.showcase_enabled.is_(True),
        )
        .order_by(AlgorithmRun.created_at.desc(), AlgorithmRun.id.desc())
        .limit(1)
    )


def _waiting_case(scenario: ShowcaseScenario, status: str = "WAITING") -> ShowcaseCase:
    stale = status == "STALE"
    return ShowcaseCase(
        key=scenario.public_key,
        title=scenario.title,
        description=scenario.description,
        source_mode="PRESET_SIMULATION",
        status=status,
        status_label="输入已变化" if stale else "等待生成",
        headline="输入已变化，等待重新测算" if stale else "等待生成分配方案",
        calculated_at=None,
        data_cutoff=None,
        data_cutoff_note="预设场景参数，无实时业务数据截止时间",
        input_summary=_scenario_summary(scenario),
        result_summary=ResultSummary(
            allocation_count=0,
            allocated_order_count=0,
            unmatched_count=0,
            warning_count=0,
        ),
        allocations=[],
        unmatched_reasons=[],
    )


def _carpool_case(db: Session, scenario: ShowcaseScenario, expected_signature: str) -> ShowcaseCase:
    run = _latest_run(db, "CARPOOL", scenario.public_key)
    if run is None:
        return _waiting_case(scenario)
    if run.input_signature != expected_signature:
        return _waiting_case(scenario, "STALE")
    order_lookup = {item.order_key: item for item in scenario.orders}
    private_keys = run.input_snapshot.get("private_order_keys", {})
    vehicle_lookup = {item.vehicle_key: item for item in scenario.vehicles}
    private_vehicles = run.input_snapshot.get("private_vehicle_keys", {})
    allocations = []
    allocated = 0
    for candidate in run.output_snapshot.get("candidates", [])[:24]:
        orders = [order_lookup[private_keys[item]] for item in candidate.get("order_ids", []) if item in private_keys]
        vehicle = vehicle_lookup.get(private_vehicles.get(candidate.get("vehicle_id")))
        departure_start = min(item.departure_at for item in orders)
        departure_end = max(item.departure_at for item in orders)
        allocations.append(
            CarpoolAllocation(
                vehicle_label=(
                    f"{enum_label(TEMPERATURE_ZONE_LABELS, candidate['temperature_zone'])}配送车"
                    f"（{round(vehicle.max_weight_kg / 1000):g} 吨级）"
                    if vehicle
                    else "配送车辆"
                ),
                temperature_zone_label=enum_label(TEMPERATURE_ZONE_LABELS, candidate["temperature_zone"]),
                enterprise_count=candidate["enterprise_count"],
                order_count=len(orders),
                product_count=candidate["product_count"],
                store_names=list(dict.fromkeys(item.store_name for item in orders)),
                departure_start=departure_start.isoformat(),
                departure_end=departure_end.isoformat(),
                total_weight_kg=candidate["total_weight_kg"],
                total_volume_m3=candidate["total_volume_m3"],
                weight_utilization_pct=candidate["weight_utilization_pct"],
                volume_utilization_pct=candidate["volume_utilization_pct"],
                capacity_utilization_pct=candidate["capacity_utilization_pct"],
                merged_route_estimated_km=candidate["merged_route_estimated_km"],
                independent_route_estimated_km=candidate["independent_route_estimated_km"],
                detour_estimated_km=candidate["detour_estimated_km"],
                mileage_benefit_estimated_km=candidate["mileage_benefit_estimated_km"],
                route_label="经纬度估算路线",
                decision_state="RECOMMENDED",
                decision_state_label="推荐方案",
                explanation=candidate["explanation"],
            ).model_dump(mode="json")
        )
        allocated += len(orders)
    unmatched = run.output_snapshot.get("unmatched", [])
    status = "READY" if allocations and not unmatched else "PARTIAL" if allocations else "NO_MATCH"
    return ShowcaseCase(
        key=scenario.public_key,
        title=scenario.title,
        description=scenario.description,
        source_mode="PRESET_SIMULATION",
        status=status,
        status_label={"READY": "形成可执行方案", "PARTIAL": "部分订单待调整", "NO_MATCH": "未形成方案"}[status],
        headline=(
            f"形成 {len(allocations)} 个配送组，{allocated} 单已分配，{len(unmatched)} 单待调整"
            if allocations
            else "本次测算未形成可执行方案"
        ),
        calculated_at=as_utc(run.created_at).isoformat(),
        data_cutoff=None,
        data_cutoff_note="预设场景参数，无实时业务数据截止时间",
        input_summary=_scenario_summary(scenario),
        result_summary=ResultSummary(
            allocation_count=len(allocations),
            allocated_order_count=allocated,
            unmatched_count=len(unmatched),
            warning_count=len(unmatched),
        ),
        allocations=allocations,
        unmatched_reasons=_reason_rows(unmatched),
    )


def _warehouse_case(db: Session, scenario: ShowcaseScenario, expected_signature: str) -> ShowcaseCase:
    run = _latest_run(db, "WAREHOUSE", scenario.public_key)
    if run is None:
        return _waiting_case(scenario)
    if run.input_signature != expected_signature:
        return _waiting_case(scenario, "STALE")
    warehouse_lookup = {item.warehouse_key: item for item in scenario.warehouses}
    private_warehouses = run.input_snapshot.get("private_warehouse_keys", {})
    allocations = []
    for group in run.output_snapshot.get("candidates", []):
        for index, candidate in enumerate(group.get("candidate_warehouses", [])):
            warehouse = warehouse_lookup.get(private_warehouses.get(candidate.get("warehouse_id")))
            allocations.append(
                WarehouseAllocation(
                    warehouse_name=warehouse.name if warehouse else "名称待补充",
                    temperature_zone_label=enum_label(TEMPERATURE_ZONE_LABELS, group["temperature_zone"]),
                    enterprise_count=group["enterprise_count"],
                    order_count=len(group.get("order_ids", [])),
                    product_count=group["product_count"],
                    required_volume_m3=group["required_volume_m3"],
                    available_volume_m3=candidate["available_volume_m3"],
                    remaining_volume_m3=candidate["remaining_volume_m3"],
                    estimated_distance_km=candidate["estimated_distance_km"],
                    inbound_start=group["inbound_window"]["start"],
                    inbound_end=group["inbound_window"]["end"],
                    outbound_start=group["outbound_window"]["start"],
                    outbound_end=group["outbound_window"]["end"],
                    eligible=candidate["eligible"],
                    decision_state="RECOMMENDED" if index == 0 and candidate["eligible"] else "ALTERNATIVE",
                    decision_state_label="推荐仓库" if index == 0 and candidate["eligible"] else "备选仓库",
                    reasons=candidate.get("reasons") or [candidate.get("reason", "满足匹配规则")],
                    route_label="经纬度估算距离",
                ).model_dump(mode="json")
            )
    unmatched = run.output_snapshot.get("unmatched", [])
    status = "READY" if allocations and not unmatched else "PARTIAL" if allocations else "NO_MATCH"
    return ShowcaseCase(
        key=scenario.public_key,
        title=scenario.title,
        description=scenario.description,
        source_mode="PRESET_SIMULATION",
        status=status,
        status_label={"READY": "形成可执行方案", "PARTIAL": "部分温区待调整", "NO_MATCH": "未形成方案"}[status],
        headline=(f"形成 {len(allocations)} 个仓库匹配结果" if allocations else "本次测算未形成可执行方案"),
        calculated_at=as_utc(run.created_at).isoformat(),
        data_cutoff=None,
        data_cutoff_note="预设场景参数，无实时业务数据截止时间",
        input_summary=_scenario_summary(scenario),
        result_summary=ResultSummary(
            allocation_count=len(allocations),
            allocated_order_count=sum(
                len(item.get("order_ids", [])) for item in run.output_snapshot.get("candidates", [])
            ),
            unmatched_count=len(unmatched),
            warning_count=len(unmatched),
        ),
        allocations=allocations[:24],
        unmatched_reasons=_reason_rows(unmatched),
    )


def _procurement_case(db: Session) -> ShowcaseCase:
    start, end = procurement_cycle()
    rows = list(
        db.scalars(
            select(ProcurementAggregation)
            .where(ProcurementAggregation.cycle_start == start)
            .order_by(ProcurementAggregation.product_id)
        )
    )
    products = {item.id: item for item in db.scalars(select(Product))}
    allocations = []
    warnings = 0
    for row in rows:
        product = products.get(row.product_id)
        recommended = (row.recommendation_snapshot or {}).get("supplier_name")
        options = sorted(
            row.candidate_snapshot,
            key=lambda item: (
                item.get("supplier_name") != recommended,
                -float(item.get("composite_score") or 0),
                float(item.get("total_amount") or 0),
                item.get("supplier_name") or "",
            ),
        )[:3]
        allocations.append(
            ProcurementAllocation(
                product_name=product.name if product else "名称待补充",
                base_unit=row.base_unit,
                cycle_start=row.cycle_start.isoformat(),
                cycle_end=row.cycle_end.isoformat(),
                automatic_quantity=row.automatic_quantity,
                effective_quantity=row.adjusted_quantity
                if row.adjusted_quantity is not None
                else row.automatic_quantity,
                quantity_source="MANUAL_ADJUSTED" if row.adjusted_quantity is not None else "AUTOMATIC",
                quantity_source_label="人工调整" if row.adjusted_quantity is not None else "自动汇总",
                confirmation_state=row.status,
                confirmation_state_label="已确认" if row.status == "CONFIRMED" else "待确认",
                supplier_options=[
                    SupplierOption(
                        supplier_name=item.get("supplier_name") or "名称待补充",
                        quantity=item["quantity"],
                        unit_price=item["unit_price"],
                        total_amount=item["total_amount"],
                        price_score=item["price_score"],
                        delivery_score=item["delivery_score"],
                        quality_score=item["quality_score"],
                        composite_score=item["composite_score"],
                        tier_min_quantity=item["tier_min_quantity"],
                        tier_max_quantity=item.get("tier_max_quantity"),
                        supply_capacity=item["supply_capacity"],
                        quote_valid_from=item["quote_valid_from"],
                        quote_valid_to=item["quote_valid_to"],
                        is_recommended=item.get("supplier_name") == recommended,
                        recommendation_reason=item["recommendation_reason"],
                    )
                    for item in options
                ],
                unit_conversion_warnings=[
                    {
                        "quantity": warning.get("quantity", 0),
                        "unit": str(warning.get("unit") or "名称待补充")[:20],
                        "reason": str(warning.get("reason") or "单位换算待确认")[:240],
                    }
                    for warning in row.unit_conversion_warnings
                ],
            ).model_dump(mode="json")
        )
        warnings += len(row.unit_conversion_warnings) + (0 if options else 1)
    status = "WAITING" if not rows else "PARTIAL" if warnings else "READY"
    cutoff = max((as_utc(row.data_cutoff) for row in rows if row.data_cutoff), default=None)
    calculated = max((as_utc(row.updated_at) for row in rows), default=None)
    return ShowcaseCase(
        key="current-procurement-cycle",
        title="本周集中采购建议",
        description="按企业需求、生产计划、缺货和库存汇总，并对有效供应商报价进行固定权重评分。",
        source_mode="CURRENT_PROCUREMENT_CYCLE",
        status=status,
        status_label="等待汇总"
        if status == "WAITING"
        else "部分商品待调整"
        if status == "PARTIAL"
        else "采购建议已生成",
        headline="等待生成本周采购汇总" if not rows else f"{len(rows)} 个商品形成采购建议",
        calculated_at=calculated.isoformat() if calculated else None,
        data_cutoff=cutoff.isoformat() if cutoff else None,
        data_cutoff_note=None if cutoff else "当前汇总没有可核验的业务数据截止时间",
        input_summary=InputSummary(
            service_area="长春市及近郊",
            enterprise_count=0,
            order_count=0,
            product_count=len(rows),
            temperature_zones=[],
            period_start=start.isoformat(),
            period_end=end.isoformat(),
        ),
        result_summary=ResultSummary(
            allocation_count=len(allocations),
            allocated_order_count=None,
            unmatched_count=sum(not item["supplier_options"] for item in allocations),
            warning_count=warnings,
        ),
        allocations=allocations[:24],
        unmatched_reasons=[],
    )


def _forecast_case(db: Session) -> ShowcaseCase:
    start, end = next_week_window()
    batch = db.scalar(select(DemandForecastBatch).where(DemandForecastBatch.forecast_start == start))
    if batch is None:
        rows: list[DemandForecastProjection] = []
    else:
        rows = list(
            db.scalars(
                select(DemandForecastProjection)
                .where(DemandForecastProjection.forecast_start == start)
                .order_by(DemandForecastProjection.product_id, DemandForecastProjection.enterprise_id)
            )
        )
    products = {item.id: item for item in db.scalars(select(Product))}
    grouped: dict[str, list[DemandForecastProjection]] = defaultdict(list)
    for row in rows:
        grouped[row.product_id].append(row)
    allocations = []
    method_order = list(FORECAST_METHOD_LABELS)
    for product_id, items in sorted(
        grouped.items(), key=lambda item: (products.get(item[0]).name if products.get(item[0]) else "", item[0])
    ):
        product = products.get(product_id)
        available = [item for item in items if item.forecast_quantity is not None]
        methods = sorted(
            {item.method for item in items},
            key=lambda value: method_order.index(value) if value in method_order else 99,
        )
        allocations.append(
            ForecastAllocation(
                product_name=product.name if product else "名称待补充",
                unit=product.unit if product else "单位待补充",
                forecast_start=start.isoformat(),
                forecast_end=end.isoformat(),
                enterprise_count=len(items),
                available_forecast_count=len(available),
                historical_usage=round(sum(item.historical_usage for item in items), 2),
                production_plan_quantity=round(sum(item.production_plan_quantity for item in items), 2),
                forecast_quantity=round(sum(item.forecast_quantity for item in available), 2) if available else None,
                suggested_purchase_quantity=round(sum(item.suggested_purchase_quantity for item in items), 2),
                lower_bound=round(sum(item.lower_bound for item in items if item.lower_bound is not None), 2)
                if any(item.lower_bound is not None for item in items)
                else None,
                upper_bound=round(sum(item.upper_bound for item in items if item.upper_bound is not None), 2)
                if any(item.upper_bound is not None for item in items)
                else None,
                mae_average=round(mean(item.mae for item in items if item.mae is not None), 2)
                if any(item.mae is not None for item in items)
                else None,
                smape_average=round(mean(item.smape for item in items if item.smape is not None), 2)
                if any(item.smape is not None for item in items)
                else None,
                method_labels=[enum_label(FORECAST_METHOD_LABELS, value) for value in methods],
                data_insufficient_count=sum(item.method == "INSUFFICIENT_DATA" for item in items),
                method_note="；".join(dict.fromkeys(item.method_note for item in items)),
            ).model_dump(mode="json")
        )
    insufficient = sum(item["data_insufficient_count"] for item in allocations)
    status = "WAITING" if batch is None else "PARTIAL" if insufficient else "READY"
    return ShowcaseCase(
        key="next-week-forecast",
        title="下周需求预测汇总",
        description="按下一个完整自然周汇总企业商品预测，保留区间、误差和数据不足说明。",
        source_mode="NEXT_WEEK_BATCH",
        status=status,
        status_label="等待预测" if status == "WAITING" else "部分数据不足" if status == "PARTIAL" else "预测已生成",
        headline="等待生成下周预测批次" if batch is None else f"{len(allocations)} 个商品形成预测汇总",
        calculated_at=as_utc(batch.updated_at).isoformat() if batch else None,
        data_cutoff=as_utc(batch.data_cutoff).isoformat() if batch and batch.data_cutoff else None,
        data_cutoff_note=None if batch and batch.data_cutoff else "当前预测没有可核验的业务数据截止时间",
        input_summary=InputSummary(
            service_area="长春市及近郊",
            enterprise_count=max((item["enterprise_count"] for item in allocations), default=0),
            order_count=0,
            product_count=len(allocations),
            temperature_zones=[],
            period_start=start.isoformat(),
            period_end=end.isoformat(),
        ),
        result_summary=ResultSummary(
            allocation_count=len(allocations),
            allocated_order_count=None,
            unmatched_count=insufficient,
            warning_count=insufficient,
        ),
        allocations=allocations[:24],
        unmatched_reasons=(
            [UnmatchedReason(reason="部分企业数据不足，已按既定降级规则处理", count=insufficient)]
            if insufficient
            else []
        ),
    )


def build_algorithm_showcase(db: Session) -> dict[str, Any]:
    catalog = load_showcase_catalog()
    carpool_cases = [_carpool_case(db, scenario, catalog_sha256(scenario)) for scenario in catalog.scenarios]
    warehouse_cases = [_warehouse_case(db, scenario, catalog_sha256(scenario)) for scenario in catalog.scenarios]
    procurement_case = _procurement_case(db)
    forecast_case = _forecast_case(db)
    panels = [
        {"kind": "CARPOOL", "title": "拼车联配", "cases": carpool_cases},
        {"kind": "WAREHOUSE", "title": "共享仓匹配", "cases": warehouse_cases},
        {"kind": "PROCUREMENT", "title": "集中采购", "cases": [procurement_case]},
        {"kind": "FORECAST", "title": "下周预测", "cases": [forecast_case]},
    ]
    for panel in panels:
        cases = panel["cases"]
        panel_status = "READY"
        if all(item.status == "WAITING" for item in cases):
            panel_status = "WAITING"
        elif any(item.status in {"PARTIAL", "NO_MATCH", "STALE"} for item in cases):
            panel_status = "PARTIAL"
        panel.update(
            {
                "status": panel_status,
                "status_label": "等待生成"
                if panel_status == "WAITING"
                else "存在待调整项"
                if panel_status == "PARTIAL"
                else "方案已生成",
                "headline": cases[0].headline,
                "calculated_at": max((item.calculated_at for item in cases if item.calculated_at), default=None),
                "data_cutoff": max((item.data_cutoff for item in cases if item.data_cutoff), default=None),
                "data_cutoff_note": next((item.data_cutoff_note for item in cases if item.data_cutoff_note), None),
                "cases": [item.model_dump(mode="json") for item in cases],
            }
        )
    panel_statuses = {item["status"] for item in panels}
    status = "WAITING" if panel_statuses == {"WAITING"} else "READY" if panel_statuses == {"READY"} else "PARTIAL"
    result = {
        "schema_version": "1.0",
        "status": status,
        "status_label": "等待生成"
        if status == "WAITING"
        else "部分方案待调整"
        if status == "PARTIAL"
        else "方案已生成",
        "data_cutoff": max((item["data_cutoff"] for item in panels if item["data_cutoff"]), default=None),
        "data_cutoff_note": "预设场景测算不代表实时业务订单",
        "panels": panels,
    }
    assert_public_showcase_safe(result)
    for panel in result["panels"]:
        for case in panel["cases"]:
            for allocation in case["allocations"]:
                ALLOCATION_ADAPTER.validate_python(allocation)
    return result
