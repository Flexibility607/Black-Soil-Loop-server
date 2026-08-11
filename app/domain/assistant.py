from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.dashboard import read_dashboard_projection
from app.domain.openai_layer import classify_intent
from app.domain.procurement import procurement_cycle
from app.shared.errors import BusinessError
from app.shared.models import (
    AlgorithmRun,
    DemandForecastBatch,
    DemandForecastProjection,
    Enterprise,
    ProcurementAggregation,
    Product,
)
from app.shared.periods import PeriodWindow, period_window
from app.shared.security import as_utc

ALLOWED_CHART_TYPES = ["bar", "line", "donut", "route"]
INTENT_KEYWORDS = {
    "FORECAST": ("预测", "下周需求", "需求区间", "mae", "smape"),
    "PROCUREMENT": ("采购", "供应商", "报价", "起订量"),
    "WAREHOUSE": ("拼仓", "仓库", "库容", "温区"),
    "CARPOOL": ("拼车", "合单", "绕行", "车辆利用率"),
    "TRANSPORT": ("运输", "路线", "车辆", "司机", "位置"),
    "ALERT": ("温度", "湿度", "报警", "异常"),
    "INVENTORY": ("库存", "缺货", "库存变化"),
    "CHANNEL": ("第三空间", "渠道", "占比"),
    "OPERATIONS": ("订单", "营业额", "销售", "经营"),
}
CHART_COMPATIBILITY = {
    "CHANNEL": {"bar", "donut"},
    "ALERT": {"bar", "donut"},
    "OPERATIONS": {"bar", "line"},
    "INVENTORY": {"bar"},
    "TRANSPORT": {"route"},
    "CARPOOL": {"bar"},
    "WAREHOUSE": {"bar"},
    "PROCUREMENT": {"bar"},
    "FORECAST": {"bar", "line"},
    "OVERVIEW": {"bar"},
}
UNSAFE_SQL = re.compile(r"(?:\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\bdrop\b|\btruncate\b)", re.I)
UNSAFE_PROMPTS = ("忽略系统", "忽略以上", "系统提示词", "开发者指令", "越权", "绕过权限")
WRITE_REQUESTS = (
    "请发布",
    "帮我发布",
    "请确认采购",
    "帮我确认",
    "请取消",
    "帮我取消",
    "请处理报警",
    "请修改",
    "帮我删除",
    "替我执行",
)
RECOMMENDED_QUESTIONS = [
    "本月第三空间营业额占比是多少？",
    "当前有哪些未处理报警？",
    "哪些商品库存偏低？",
    "最新运输任务在哪里？",
    "最近一次拼车方案的利用率如何？",
    "最近一次采购推荐是什么？",
]


def _validate_read_only_question(question: str) -> None:
    normalized = question.strip().lower()
    if UNSAFE_SQL.search(normalized) or any(value in normalized for value in UNSAFE_PROMPTS + WRITE_REQUESTS):
        raise BusinessError(
            "ASSISTANT_QUERY_NOT_ALLOWED",
            "助手仅支持白名单业务数据查询，不能执行指令、SQL 或业务写操作",
            status_code=400,
        )


def _local_intent(question: str) -> str:
    normalized = question.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return intent
    return "OVERVIEW"


def _latest_algorithm(db: Session, intent: str, window: PeriodWindow) -> AlgorithmRun | None:
    algorithm_type = "DEMAND_FORECAST" if intent == "FORECAST" else intent
    return db.scalar(
        select(AlgorithmRun)
        .where(
            AlgorithmRun.algorithm_type == algorithm_type,
            AlgorithmRun.created_at >= window.start_at,
            AlgorithmRun.created_at < window.end_at,
        )
        .order_by(AlgorithmRun.created_at.desc())
    )


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _carpool_rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, candidate in enumerate(output.get("candidates", [])[:8], start=1):
        if not isinstance(candidate, dict):
            continue
        rows.append(
            {
                "name": f"方案 {index}",
                "enterprise_count": int(candidate.get("enterprise_count") or 0),
                "order_count": len(candidate.get("order_nos") or []),
                "plate_no": candidate.get("plate_no"),
                "temperature_zone": candidate.get("temperature_zone"),
                "capacity_utilization_pct": _number(candidate.get("capacity_utilization_pct")),
                "detour_estimated_km": _number(candidate.get("detour_estimated_km")),
                "mileage_benefit_estimated_km": _number(candidate.get("mileage_benefit_estimated_km")),
                "route_label": "经纬度估算路线",
                "matched": True,
                "mismatch_reasons": [],
            }
        )
    for item in output.get("unmatched", [])[:8]:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "未记录不匹配原因")[:240]
        rows.append(
            {
                "name": item.get("order_no") or "未匹配订单",
                "enterprise_count": 0,
                "order_count": 1,
                "plate_no": None,
                "temperature_zone": None,
                "capacity_utilization_pct": None,
                "detour_estimated_km": None,
                "mileage_benefit_estimated_km": None,
                "route_label": "经纬度估算路线",
                "matched": False,
                "mismatch_reasons": [reason],
            }
        )
    return rows


def _warehouse_rows(output: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for group in output.get("candidates", [])[:8]:
        if not isinstance(group, dict):
            continue
        for warehouse in group.get("candidate_warehouses", [])[:3]:
            if not isinstance(warehouse, dict):
                continue
            rows.append(
                {
                    "name": warehouse.get("warehouse_name") or "未命名仓库",
                    "temperature_zone": group.get("temperature_zone"),
                    "required_volume_m3": _number(group.get("required_volume_m3")),
                    "available_volume_m3": _number(warehouse.get("available_volume_m3")),
                    "estimated_distance_km": _number(warehouse.get("estimated_distance_km")),
                    "eligible": bool(warehouse.get("eligible")),
                    "reason": str(warehouse.get("reason") or "暂无说明")[:240],
                    "inbound_window": {
                        "start": (group.get("inbound_window") or {}).get("start"),
                        "end": (group.get("inbound_window") or {}).get("end"),
                    },
                    "outbound_window": {
                        "start": (group.get("outbound_window") or {}).get("start"),
                        "end": (group.get("outbound_window") or {}).get("end"),
                    },
                    "route_label": "经纬度估算路线",
                }
            )
    return rows[:8]


def _algorithm_chart(
    db: Session,
    intent: str,
    window: PeriodWindow,
) -> tuple[str, dict[str, Any], str, Any]:
    latest = _latest_algorithm(db, intent, window)
    if latest is None:
        return (
            "当前统计周期内没有对应的算法运行记录。",
            {"type": "bar", "title": "算法运行摘要", "data": []},
            "项",
            None,
        )
    output = latest.output_snapshot if isinstance(latest.output_snapshot, dict) else {}
    if intent == "CARPOOL":
        rows = _carpool_rows(output)
        unit = "%"
        value_key = "capacity_utilization_pct"
        answer = f"最近一次拼车计算形成 {len(rows)} 个公开候选方案。"
    elif intent == "WAREHOUSE":
        rows = _warehouse_rows(output)
        unit = "立方米"
        value_key = "available_volume_m3"
        answer = f"最近一次拼仓计算形成 {len(rows)} 条公开仓库匹配结果。"
    else:
        raise RuntimeError(f"不支持的算法助手意图: {intent}")
    return (
        answer,
        {
            "type": "bar",
            "title": {
                "CARPOOL": "拼车方案利用率",
                "WAREHOUSE": "拼仓可用库容",
            }[intent],
            "data": rows,
            "category_key": "name",
            "value_key": value_key,
        },
        unit,
        None,
    )


def _procurement_chart(db: Session) -> tuple[str, dict[str, Any], str, Any]:
    cycle_start, cycle_end = procurement_cycle()
    aggregations = list(
        db.scalars(
            select(ProcurementAggregation)
            .where(ProcurementAggregation.cycle_start == cycle_start)
            .order_by(ProcurementAggregation.product_id)
        )
    )
    if not aggregations:
        return (
            "当前采购周期尚未生成自动采购汇总。",
            {"type": "bar", "title": "供应商方案对比", "data": []},
            "分",
            None,
        )
    products = {
        item.id: item
        for item in db.scalars(
            select(Product).where(Product.id.in_({row.product_id for row in aggregations}))
        )
    }
    rows: list[dict[str, Any]] = []
    for aggregation in aggregations:
        product = products.get(aggregation.product_id)
        warnings = [
            {
                "quantity": _number(warning.get("quantity")),
                "unit": warning.get("unit"),
                "reason": str(warning.get("reason") or "单位无法换算")[:240],
            }
            for warning in (aggregation.unit_conversion_warnings or [])
            if isinstance(warning, dict)
        ]
        candidates = (aggregation.candidate_snapshot or [])[:3]
        if not candidates:
            candidates = [{}]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            supplier_name = candidate.get("supplier_name") or "暂无符合条件的供应商"
            rows.append(
                {
                    "name": f"{product.name if product else '未命名商品'} · {supplier_name}",
                    "product_name": product.name if product else "未命名商品",
                    "base_unit": aggregation.base_unit,
                    "cycle_start": cycle_start.isoformat(),
                    "cycle_end": cycle_end.isoformat(),
                    "automatic_quantity": _number(aggregation.automatic_quantity),
                    "adjusted_quantity": _number(aggregation.adjusted_quantity),
                    "effective_quantity": _number(
                        aggregation.adjusted_quantity
                        if aggregation.adjusted_quantity is not None
                        else aggregation.automatic_quantity
                    ),
                    "confirmation_status": aggregation.status,
                    "supplier_name": supplier_name,
                    "quantity": _number(candidate.get("quantity")),
                    "unit_price": _number(candidate.get("unit_price")),
                    "total_amount": _number(candidate.get("total_amount")),
                    "delivery_score": _number(candidate.get("delivery_score")),
                    "quality_score": _number(candidate.get("quality_score")),
                    "composite_score": _number(candidate.get("composite_score")),
                    "tier_min_quantity": _number(candidate.get("tier_min_quantity")),
                    "tier_max_quantity": _number(candidate.get("tier_max_quantity")),
                    "supply_capacity": _number(candidate.get("supply_capacity")),
                    "quote_valid_from": candidate.get("quote_valid_from"),
                    "quote_valid_to": candidate.get("quote_valid_to"),
                    "recommendation_reason": str(
                        candidate.get("recommendation_reason") or "暂无推荐理由"
                    )[:240],
                    "unit_conversion_warnings": warnings,
                }
            )
    cutoff = max(
        (as_utc(item.data_cutoff) for item in aggregations if item.data_cutoff is not None),
        default=None,
    )
    return (
        f"{cycle_start.isoformat()} 至 {cycle_end.isoformat()} 已形成 {len(aggregations)} 个商品采购汇总。",
        {
            "type": "bar",
            "title": "供应商方案对比",
            "data": rows[:24],
            "category_key": "name",
            "value_key": "composite_score",
        },
        "分",
        cutoff,
    )


def _forecast_chart(
    db: Session,
    window: PeriodWindow,
) -> tuple[str, dict[str, Any], str, Any]:
    batch = db.scalar(
        select(DemandForecastBatch)
        .where(
            DemandForecastBatch.created_at >= window.start_at,
            DemandForecastBatch.created_at < window.end_at,
            DemandForecastBatch.status == "READY",
        )
        .order_by(DemandForecastBatch.created_at.desc())
    )
    if batch is None:
        return (
            "当前统计周期内尚未生成可用的下周预测批次。",
            {"type": "line", "title": "下周需求预测", "data": []},
            "商品基础单位",
            None,
        )
    projections = list(
        db.scalars(
            select(DemandForecastProjection)
            .where(DemandForecastProjection.forecast_start == batch.forecast_start)
            .order_by(DemandForecastProjection.product_id, DemandForecastProjection.enterprise_id)
        )
    )
    enterprise_ids = {item.enterprise_id for item in projections}
    product_ids = {item.product_id for item in projections}
    enterprises = {
        item.id: item for item in db.scalars(select(Enterprise).where(Enterprise.id.in_(enterprise_ids)))
    }
    products = {item.id: item for item in db.scalars(select(Product).where(Product.id.in_(product_ids)))}
    rows = [
        {
            "name": products[item.product_id].name if item.product_id in products else "未命名商品",
            "enterprise_name": (
                enterprises[item.enterprise_id].name if item.enterprise_id in enterprises else "未命名企业"
            ),
            "unit": products[item.product_id].unit if item.product_id in products else None,
            "forecast_start": item.forecast_start.isoformat(),
            "forecast_end": item.forecast_end.isoformat(),
            "historical_usage": _number(item.historical_usage),
            "production_plan_quantity": _number(item.production_plan_quantity),
            "forecast_quantity": _number(item.forecast_quantity),
            "suggested_purchase_quantity": _number(item.suggested_purchase_quantity),
            "lower_bound": _number(item.lower_bound),
            "upper_bound": _number(item.upper_bound),
            "mae": _number(item.mae),
            "smape": _number(item.smape),
            "method": item.method,
            "method_note": str(item.method_note)[:240],
            "data_cutoff": item.data_cutoff.isoformat() if item.data_cutoff else None,
        }
        for item in projections[:40]
    ]
    return (
        (
            f"{batch.forecast_start.isoformat()} 至 {batch.forecast_end.isoformat()} 的下周预测"
            f"覆盖 {len(projections)} 个企业商品组合。"
        ),
        {
            "type": "line",
            "title": "下周需求预测",
            "data": rows,
            "category_key": "name",
            "value_key": "forecast_quantity",
        },
        "商品基础单位",
        as_utc(batch.data_cutoff) if batch.data_cutoff is not None else None,
    )


def _inventory_rows(rows: Any) -> list[dict[str, Any]]:
    return [
        {"product_name": item.get("product_name") or "未命名商品", "quantity": _number(item.get("quantity"))}
        for item in rows
        if isinstance(item, dict)
    ]


def _enterprise_demand_rows(rows: Any) -> list[dict[str, Any]]:
    return [
        {"name": item.get("name") or "未命名企业", "quantity_kg": _number(item.get("quantity_kg"))}
        for item in rows
        if isinstance(item, dict)
    ]


def _public_routes(rows: Any, alerts: Any) -> list[dict[str, Any]]:
    alerts_by_task: dict[Any, list[dict[str, Any]]] = {}
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        alerts_by_task.setdefault(alert.get("task_id"), []).append(
            {
                "alert_type": alert.get("alert_type"),
                "status": alert.get("status"),
                "message": str(alert.get("message") or "暂无报警说明")[:240],
                "opened_at": alert.get("opened_at"),
            }
        )
    safe_rows = []
    for route in rows:
        if not isinstance(route, dict):
            continue
        safe_rows.append(
            {
                "task_no": route.get("task_no"),
                "status": route.get("status"),
                "plate_no": route.get("plate_no"),
                "temperature_zone": route.get("temperature_zone"),
                "origin": {
                    "latitude": (route.get("origin") or {}).get("latitude"),
                    "longitude": (route.get("origin") or {}).get("longitude"),
                },
                "destination": {
                    "latitude": (route.get("destination") or {}).get("latitude"),
                    "longitude": (route.get("destination") or {}).get("longitude"),
                },
                "stops": [
                    {
                        "store_name": stop.get("store_name"),
                        "channel": stop.get("channel"),
                        "latitude": stop.get("latitude"),
                        "longitude": stop.get("longitude"),
                        "status": stop.get("status"),
                    }
                    for stop in route.get("stops", [])
                    if isinstance(stop, dict)
                ],
                "latest_telemetry": {
                    key: (route.get("latest_telemetry") or {}).get(key)
                    for key in ("temperature_c", "humidity_pct", "sampled_at", "anomaly_code")
                }
                if route.get("latest_telemetry")
                else None,
                "latest_location": {
                    key: (route.get("latest_location") or {}).get(key)
                    for key in ("latitude", "longitude", "speed_mps", "accuracy_m", "recorded_at")
                }
                if route.get("latest_location")
                else None,
                "alerts": alerts_by_task.get(route.get("task_id"), []),
                "alert_source": "独立温湿度报警投影",
                "route_label": "经纬度估算路线",
            }
        )
    return safe_rows


def answer_data_question(
    db: Session,
    question: str,
    preferred_chart: str = "auto",
    period: str = "30d",
    *,
    allow_external_classifier: bool = True,
) -> dict[str, Any]:
    _validate_read_only_question(question)
    window = period_window(period)
    snapshot = read_dashboard_projection(db, period)
    if snapshot is None:
        raise BusinessError("DASHBOARD_NOT_READY", "看板投影尚未生成", status_code=503)
    local_intent = _local_intent(question)
    remote_intent = None
    if local_intent == "OVERVIEW" and allow_external_classifier:
        remote_intent = classify_intent(question)
    intent = remote_intent or local_intent
    charts = snapshot["charts"]
    chart: dict[str, Any]
    unit: str
    data_cutoff = snapshot["data_cutoff"]

    if intent == "CHANNEL":
        answer = f"第三空间销售额占比为 {snapshot['summary']['third_space_sales_share_pct']}%。"
        chart = {
            "type": "donut",
            "title": "渠道销售额占比",
            "data": charts["channel_mix"],
            "category_key": "channel",
            "value_key": "sales_amount",
        }
        unit = "人民币"
    elif intent == "ALERT":
        rows = [
            {"name": item["alert_type"], "value": 1}
            for item in snapshot["alerts"]
            if item["status"] == "OPEN"
        ]
        answer = f"当前共有 {snapshot['summary']['open_alert_count']} 条未处理运输报警。"
        chart = {"type": "bar", "title": "运输报警", "data": rows, "category_key": "name", "value_key": "value"}
        unit = "条"
    elif intent == "OPERATIONS":
        answer = (
            f"{window.start_date.isoformat()} 至 {window.end_date.isoformat()} 的授权营业额合计 "
            f"{snapshot['summary']['total_sales_amount']:.2f} 元。"
        )
        chart = {
            "type": "line",
            "title": "每日订单与营业额",
            "data": charts["daily_operations"],
            "category_key": "date",
            "value_keys": ["order_count", "sales_amount"],
        }
        unit = "人民币"
    elif intent == "INVENTORY":
        answer = f"当前有 {snapshot['summary']['low_stock_count']} 个门店商品库存低于阈值。"
        chart = {
            "type": "bar",
            "title": "商品库存汇总",
            "data": _inventory_rows(charts["inventory_by_product"]),
            "category_key": "product_name",
            "value_key": "quantity",
        }
        unit = "商品基础单位"
    elif intent == "TRANSPORT":
        answer = f"当前有 {snapshot['summary']['active_transport_count']} 条未完成运输任务，地图路线均为经纬度估算。"
        chart = {
            "type": "route",
            "title": "运输任务估算路线",
            "data": _public_routes(snapshot["map"]["routes"], snapshot["alerts"]),
        }
        unit = "项"
    elif intent in {"CARPOOL", "WAREHOUSE"}:
        answer, chart, unit, data_cutoff = _algorithm_chart(db, intent, window)
    elif intent == "PROCUREMENT":
        answer, chart, unit, data_cutoff = _procurement_chart(db)
    elif intent == "FORECAST":
        answer, chart, unit, data_cutoff = _forecast_chart(db, window)
    else:
        intent = "OVERVIEW"
        answer = (
            f"当前汇总覆盖 {snapshot['summary']['enterprise_count']} 家企业、"
            f"{snapshot['summary']['third_space_count']} 家第三空间，"
            f"授权销售额 {snapshot['summary']['total_sales_amount']:.2f} 元。"
        )
        chart = {
            "type": "bar",
            "title": "企业需求量",
            "data": _enterprise_demand_rows(charts["demand_by_enterprise"]),
            "category_key": "name",
            "value_key": "quantity_kg",
        }
        unit = "公斤"

    fallback_reason = None
    if preferred_chart != "auto":
        if preferred_chart in CHART_COMPATIBILITY[intent]:
            chart["type"] = preferred_chart
        else:
            fallback_reason = f"{intent} 数据不支持 {preferred_chart}，已使用默认图表"
    return {
        "answer": answer,
        "chart": chart,
        "intent": intent,
        "mode": "openai_intent+deterministic" if remote_intent else "deterministic",
        "period": period,
        "period_start": window.start_date.isoformat(),
        "period_end": window.end_date.isoformat(),
        "unit": unit,
        "allowed_chart_types": ALLOWED_CHART_TYPES,
        "chart_fallback_reason": fallback_reason,
        "recommended_questions": RECOMMENDED_QUESTIONS if intent == "OVERVIEW" else [],
        "data_cutoff": data_cutoff,
    }
