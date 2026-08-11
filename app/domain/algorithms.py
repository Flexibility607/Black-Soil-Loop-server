from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.shared.models import (
    DemandHistory,
    Product,
    ProductionPlan,
    Supplier,
    SupplierPriceTier,
    TransportOrder,
    Vehicle,
    Warehouse,
)

CARPOOL_RULES = {
    "origin_max_km": 20.0,
    "destination_max_km": 25.0,
    "departure_window_minutes": 60,
    "capacity_utilization_limit": 1.0,
}

WAREHOUSE_RULES = {
    "distance_max_km": 30.0,
    "capacity_utilization_limit": 0.9,
    "reservation_ttl_minutes": 30,
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _carpool_compatible(anchor: TransportOrder, candidate: TransportOrder) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    origin_distance = haversine_km(
        anchor.origin_latitude,
        anchor.origin_longitude,
        candidate.origin_latitude,
        candidate.origin_longitude,
    )
    destination_distance = haversine_km(
        anchor.destination_latitude,
        anchor.destination_longitude,
        candidate.destination_latitude,
        candidate.destination_longitude,
    )
    departure_delta = abs((anchor.departure_at - candidate.departure_at).total_seconds()) / 60
    if origin_distance > CARPOOL_RULES["origin_max_km"]:
        reasons.append(f"始发地相距 {origin_distance:.1f} km，超过 20 km")
    if destination_distance > CARPOOL_RULES["destination_max_km"]:
        reasons.append(f"目的地相距 {destination_distance:.1f} km，超过 25 km")
    if departure_delta > CARPOOL_RULES["departure_window_minutes"]:
        reasons.append(f"发车时间相差 {departure_delta:.0f} 分钟，超过 60 分钟")
    if anchor.temperature_zone != candidate.temperature_zone:
        reasons.append("货物温区不兼容")
    return not reasons, reasons


def _pairwise_group_check(group: list[TransportOrder], candidate: TransportOrder) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for existing in group:
        compatible, pair_reasons = _carpool_compatible(existing, candidate)
        if not compatible:
            reasons.extend(f"与订单 {existing.order_no}：{reason}" for reason in pair_reasons)
    return not reasons, reasons


def _nearest_neighbor_points(
    points: list[dict[str, Any]],
    start: tuple[float, float] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    remaining = sorted(points, key=lambda item: item["stable_id"])
    ordered: list[dict[str, Any]] = []
    total = 0.0
    current = start
    while remaining:
        if current is None:
            chosen = remaining[0]
        else:
            chosen = min(
                remaining,
                key=lambda item: (
                    haversine_km(current[0], current[1], item["latitude"], item["longitude"]),
                    item["stable_id"],
                ),
            )
            total += haversine_km(current[0], current[1], chosen["latitude"], chosen["longitude"])
        ordered.append(chosen)
        current = (chosen["latitude"], chosen["longitude"])
        remaining.remove(chosen)
    return ordered, total


def _carpool_route(group: list[TransportOrder]) -> dict[str, Any]:
    pickups = [
        {
            "stable_id": order.id,
            "type": "PICKUP",
            "order_id": order.id,
            "latitude": order.origin_latitude,
            "longitude": order.origin_longitude,
        }
        for order in group
    ]
    ordered_pickups, pickup_distance = _nearest_neighbor_points(pickups)
    stores: dict[str, dict[str, Any]] = {}
    for order in group:
        stores.setdefault(
            order.store_id,
            {
                "stable_id": order.store_id,
                "type": "DELIVERY",
                "store_id": order.store_id,
                "latitude": order.destination_latitude,
                "longitude": order.destination_longitude,
            },
        )
    last_pickup = ordered_pickups[-1]
    ordered_deliveries, delivery_distance = _nearest_neighbor_points(
        list(stores.values()),
        (last_pickup["latitude"], last_pickup["longitude"]),
    )
    merged_distance = pickup_distance + delivery_distance
    independent_distance = sum(
        haversine_km(
            order.origin_latitude,
            order.origin_longitude,
            order.destination_latitude,
            order.destination_longitude,
        )
        for order in group
    )
    final_delivery = ordered_deliveries[-1]
    direct_span = haversine_km(
        ordered_pickups[0]["latitude"],
        ordered_pickups[0]["longitude"],
        final_delivery["latitude"],
        final_delivery["longitude"],
    )
    return {
        "pickup_points": ordered_pickups,
        "delivery_points": ordered_deliveries,
        "route_points": [*ordered_pickups, *ordered_deliveries],
        "merged_route_estimated_km": round(merged_distance, 2),
        "independent_route_estimated_km": round(independent_distance, 2),
        "detour_estimated_km": round(max(merged_distance - direct_span, 0.0), 2),
        "mileage_benefit_estimated_km": round(independent_distance - merged_distance, 2),
    }


def _carpool_stops(group: list[TransportOrder], delivery_order: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stops_by_store: dict[str, dict[str, Any]] = {}
    for order in group:
        stop = stops_by_store.setdefault(
            order.store_id,
            {
                "store_id": order.store_id,
                "latitude": order.destination_latitude,
                "longitude": order.destination_longitude,
                "delivery_lines": [],
            },
        )
        existing_line = next(
            (line for line in stop["delivery_lines"] if line["product_id"] == order.product_id),
            None,
        )
        if existing_line:
            existing_line["order_ids"].append(order.id)
            existing_line["expected_quantity"] += order.quantity
        else:
            stop["delivery_lines"].append(
                {
                    "order_ids": [order.id],
                    "product_id": order.product_id,
                    "expected_quantity": order.quantity,
                    "unit": order.unit,
                }
            )
    return [stops_by_store[item["store_id"]] for item in delivery_order]


def _carpool_candidate(group: list[TransportOrder], vehicle: Vehicle) -> dict[str, Any]:
    group = sorted(group, key=lambda item: (item.departure_at, item.id))
    total_weight = sum(order.weight_kg for order in group)
    total_volume = sum(order.volume_m3 for order in group)
    weight_utilization = total_weight / vehicle.max_weight_kg * 100
    volume_utilization = total_volume / vehicle.max_volume_m3 * 100
    utilization = (weight_utilization + volume_utilization) / 2
    route = _carpool_route(group)
    pairwise_checks: list[dict[str, Any]] = []
    for left_index, left in enumerate(group):
        for right in group[left_index + 1 :]:
            pairwise_checks.append(
                {
                    "left_order_id": left.id,
                    "right_order_id": right.id,
                    "origin_distance_km": round(
                        haversine_km(
                            left.origin_latitude,
                            left.origin_longitude,
                            right.origin_latitude,
                            right.origin_longitude,
                        ),
                        2,
                    ),
                    "destination_distance_km": round(
                        haversine_km(
                            left.destination_latitude,
                            left.destination_longitude,
                            right.destination_latitude,
                            right.destination_longitude,
                        ),
                        2,
                    ),
                }
            )
    departure_span = (
        max(order.departure_at for order in group) - min(order.departure_at for order in group)
    ).total_seconds() / 60
    stops = _carpool_stops(group, route["delivery_points"])
    benefit = route["mileage_benefit_estimated_km"]
    return {
        "order_ids": [order.id for order in group],
        "order_versions": {order.id: order.object_version for order in group},
        "order_nos": [order.order_no for order in group],
        "vehicle_id": vehicle.id,
        "driver_id": vehicle.driver_id,
        "plate_no": vehicle.plate_no,
        "temperature_zone": group[0].temperature_zone,
        "planned_departure_at": min(order.departure_at for order in group).isoformat(),
        "departure_span_minutes": round(departure_span, 1),
        "origin": {
            "latitude": route["pickup_points"][0]["latitude"],
            "longitude": route["pickup_points"][0]["longitude"],
        },
        "destination": {
            "latitude": route["delivery_points"][-1]["latitude"],
            "longitude": route["delivery_points"][-1]["longitude"],
        },
        "stops": stops,
        "total_weight_kg": round(total_weight, 2),
        "total_volume_m3": round(total_volume, 3),
        "weight_utilization_pct": round(weight_utilization, 1),
        "volume_utilization_pct": round(volume_utilization, 1),
        "capacity_utilization_pct": round(utilization, 1),
        "enterprise_count": len({order.enterprise_id for order in group}),
        "product_count": len({order.product_id for order in group}),
        "pairwise_checks": pairwise_checks,
        **{key: value for key, value in route.items() if key not in {"pickup_points", "delivery_points"}},
        "route_label": "经纬度估算路线",
        "explanation": (
            "候选组两两始发地、目的地、发车跨度和温区均满足约束；"
            f"载重与容积平均利用率 {utilization:.1f}%；"
            f"相对独立运输估算节省 {benefit:.2f} km，绕行 {route['detour_estimated_km']:.2f} km"
        ),
    }


def run_carpool(orders: list[TransportOrder], vehicles: list[Vehicle]) -> dict[str, Any]:
    """Build deterministic multi-order groups and keep every rejection explainable."""
    available = sorted(
        (order for order in orders if order.status == "DRAFT"),
        key=lambda item: (item.departure_at, item.id),
    )
    remaining = {order.id: order for order in available}
    candidates: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    while remaining:
        anchor = min(remaining.values(), key=lambda item: (item.departure_at, item.id))
        group = [anchor]
        rejection_reasons: dict[str, list[str]] = {}
        for order in sorted(remaining.values(), key=lambda item: (item.departure_at, item.id)):
            if order.id == anchor.id:
                continue
            compatible, reasons = _pairwise_group_check(group, order)
            if compatible:
                group.append(order)
            else:
                rejection_reasons[order.id] = reasons

        compatible_vehicles = [
            vehicle
            for vehicle in vehicles
            if vehicle.enabled and vehicle.driver_id and vehicle.temperature_zone == anchor.temperature_zone
        ]
        options: list[dict[str, Any]] = []
        for vehicle in sorted(compatible_vehicles, key=lambda item: item.id):
            selected: list[TransportOrder] = []
            weight = 0.0
            volume = 0.0
            for order in sorted(group, key=lambda item: (item.departure_at, item.id)):
                if (
                    weight + order.weight_kg <= vehicle.max_weight_kg * CARPOOL_RULES["capacity_utilization_limit"]
                    and volume + order.volume_m3 <= vehicle.max_volume_m3 * CARPOOL_RULES["capacity_utilization_limit"]
                ):
                    selected.append(order)
                    weight += order.weight_kg
                    volume += order.volume_m3
            if len(selected) >= 2:
                options.append(_carpool_candidate(selected, vehicle))

        if options:
            options.sort(
                key=lambda item: (
                    -item["enterprise_count"],
                    -len(item["order_ids"]),
                    -item["capacity_utilization_pct"],
                    -item["mileage_benefit_estimated_km"],
                    item["detour_estimated_km"],
                    item["vehicle_id"],
                    tuple(item["order_ids"]),
                )
            )
            chosen_candidate = options[0]
            candidates.append(chosen_candidate)
            for order_id in chosen_candidate["order_ids"]:
                remaining.pop(order_id, None)
        else:
            reason = "没有可承载至少两单的同温区车辆"
            if not compatible_vehicles:
                reason = "没有可用的同温区车辆和司机"
            elif len(group) == 1 and rejection_reasons:
                first = next(iter(rejection_reasons.values()))
                reason = "；".join(first)
            unmatched.append({"order_id": anchor.id, "order_no": anchor.order_no, "reason": reason})
            remaining.pop(anchor.id, None)

    candidates.sort(
        key=lambda item: (
            -item["enterprise_count"],
            -len(item["order_ids"]),
            -item["capacity_utilization_pct"],
            -item["mileage_benefit_estimated_km"],
            item["detour_estimated_km"],
            item["vehicle_id"],
            tuple(item["order_ids"]),
        )
    )
    unmatched.sort(key=lambda item: (item["order_id"], item["reason"]))

    return {
        "rules_version": "carpool-balanced-v2",
        "rules": CARPOOL_RULES,
        "candidates": candidates,
        "unmatched": unmatched,
    }


def _warehouse_windows(order: TransportOrder) -> tuple[Any, Any, Any, Any, bool]:
    explicit = (
        order.warehouse_inbound_start,
        order.warehouse_inbound_end,
        order.warehouse_outbound_start,
        order.warehouse_outbound_end,
    )
    if all(value is not None for value in explicit):
        return (*explicit, False)
    return (
        order.departure_at - timedelta(hours=6),
        order.departure_at - timedelta(hours=4),
        order.departure_at - timedelta(hours=2),
        order.departure_at,
        True,
    )


def run_warehouse_pool(orders: list[TransportOrder], warehouses: list[Warehouse]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[TransportOrder]] = defaultdict(list)
    for order in orders:
        if order.status == "DRAFT":
            grouped[(order.scenario_code, order.temperature_zone)].append(order)

    for (scenario_code, temperature_zone), group in sorted(grouped.items()):
        group = sorted(group, key=lambda item: item.id)
        volume = sum(order.volume_m3 for order in group)
        windows = [_warehouse_windows(order) for order in group]
        inbound_start = max(item[0] for item in windows)
        inbound_end = min(item[1] for item in windows)
        outbound_start = max(item[2] for item in windows)
        outbound_end = min(item[3] for item in windows)
        inbound_overlap = inbound_start < inbound_end
        outbound_overlap = outbound_start < outbound_end
        derived_windows = any(item[4] for item in windows)
        ranked: list[dict[str, Any]] = []
        for warehouse in sorted(warehouses, key=lambda item: item.id):
            distances = [
                {
                    "order_id": order.id,
                    "distance_km": round(
                        haversine_km(
                            order.origin_latitude,
                            order.origin_longitude,
                            warehouse.latitude,
                            warehouse.longitude,
                        ),
                        2,
                    ),
                }
                for order in group
            ]
            maximum_distance = max(item["distance_km"] for item in distances)
            available = max(
                warehouse.capacity_m3 * WAREHOUSE_RULES["capacity_utilization_limit"]
                - warehouse.used_m3
                - warehouse.reserved_m3,
                0.0,
            )
            reasons: list[str] = []
            if warehouse.temperature_zone != temperature_zone:
                reasons.append("仓库温区与货物温区不兼容")
            distant = [item for item in distances if item["distance_km"] > WAREHOUSE_RULES["distance_max_km"]]
            if distant:
                reasons.append(f"{len(distant)} 个订单到仓距离超过 30 km")
            if not inbound_overlap:
                reasons.append("候选订单入仓时间窗没有交集")
            if not outbound_overlap:
                reasons.append("候选订单出仓时间窗没有交集")
            if volume > available:
                reasons.append("预占后将超过仓库总库容的 90%")
            eligible = not reasons
            ranked.append(
                {
                    "warehouse_id": warehouse.id,
                    "warehouse_name": warehouse.name,
                    "warehouse_object_version": warehouse.object_version,
                    "estimated_distance_km": maximum_distance,
                    "order_distances": distances,
                    "available_volume_m3": round(available, 2),
                    "remaining_volume_m3": round(max(available - volume, 0.0), 2),
                    "eligible": eligible,
                    "reasons": reasons,
                    "reason": "满足时间窗、距离、温区和库容规则" if eligible else "；".join(reasons),
                }
            )
        ranked.sort(
            key=lambda item: (
                not item["eligible"],
                item["estimated_distance_km"],
                -item["remaining_volume_m3"],
                item["warehouse_id"],
            )
        )
        item = {
            "scenario_code": scenario_code,
            "temperature_zone": temperature_zone,
            "order_ids": [order.id for order in group],
            "order_versions": {order.id: order.object_version for order in group},
            "enterprise_count": len({order.enterprise_id for order in group}),
            "product_count": len({order.product_id for order in group}),
            "required_volume_m3": round(volume, 2),
            "inbound_window": {"start": inbound_start.isoformat(), "end": inbound_end.isoformat()},
            "outbound_window": {"start": outbound_start.isoformat(), "end": outbound_end.isoformat()},
            "used_derived_windows": derived_windows,
            "candidate_warehouses": ranked,
            "route_label": "经纬度估算路线",
        }
        if ranked and ranked[0]["eligible"]:
            candidates.append(item)
        else:
            unmatched.append({**item, "reason": "没有同时满足温区、距离和库容的仓库"})
    return {
        "rules_version": "warehouse-balanced-v2",
        "rules": WAREHOUSE_RULES,
        "candidates": candidates,
        "unmatched": unmatched,
    }


def run_procurement(db: Session, product_id: str, required_quantity: float, as_of: date) -> dict[str, Any]:
    product = db.get(Product, product_id)
    tiers = list(db.scalars(select(SupplierPriceTier).where(SupplierPriceTier.product_id == product_id)))
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    eligible: list[tuple[SupplierPriceTier, Supplier]] = []
    for tier in tiers:
        supplier = db.get(Supplier, tier.supplier_id)
        reasons: list[str] = []
        if not (tier.valid_from <= as_of <= tier.valid_to):
            reasons.append("报价不在采购周期有效期内")
        if required_quantity < tier.min_quantity:
            reasons.append("采购量低于起订量")
        if tier.max_quantity is not None and required_quantity > tier.max_quantity:
            reasons.append("采购量超过当前报价最大档位")
        if required_quantity > tier.supply_capacity:
            reasons.append("采购量超过供应商供货能力")
        if reasons:
            excluded.append(
                {
                    "supplier_id": tier.supplier_id,
                    "supplier_name": supplier.name if supplier else None,
                    "tier_min_quantity": tier.min_quantity,
                    "tier_max_quantity": tier.max_quantity,
                    "supply_capacity": tier.supply_capacity,
                    "reasons": reasons,
                }
            )
        elif supplier is not None:
            eligible.append((tier, supplier))

    prices = [float(tier.unit_price) for tier, _ in eligible]
    if prices:
        low, high = min(prices), max(prices)
        for tier, supplier in eligible:
            price = float(tier.unit_price)
            price_score = 100.0 if high == low else 100.0 * (high - price) / (high - low)
            total_score = price_score * 0.6 + supplier.delivery_score * 0.2 + supplier.quality_score * 0.2
            candidates.append(
                {
                    "supplier_id": supplier.id,
                    "supplier_name": supplier.name,
                    "quantity": required_quantity,
                    "unit_price": price,
                    "total_amount": round(price * required_quantity, 2),
                    "price_score": round(price_score, 2),
                    "delivery_score": supplier.delivery_score,
                    "quality_score": supplier.quality_score,
                    "composite_score": round(total_score, 2),
                    "tier_min_quantity": tier.min_quantity,
                    "tier_max_quantity": tier.max_quantity,
                    "supply_capacity": tier.supply_capacity,
                    "quote_valid_from": tier.valid_from.isoformat(),
                    "quote_valid_to": tier.valid_to.isoformat(),
                    "recommendation_reason": "满足报价有效期、起订量、最大档位和供货能力后综合评分",
                }
            )
    candidates.sort(
        key=lambda item: (-item["composite_score"], item["total_amount"], item["supplier_id"])
    )
    excluded.sort(key=lambda item: (item["supplier_id"], item["tier_min_quantity"]))
    return {
        "rules_version": "procurement-value-v2",
        "product_id": product_id,
        "product_name": product.name if product else None,
        "required_quantity": required_quantity,
        "weights": {"price": 0.6, "delivery": 0.2, "quality": 0.2},
        "candidates": candidates,
        "excluded_candidates": excluded,
        "recommendation": candidates[0] if candidates else None,
        "reason": "先满足起订量、供货能力和阶梯价，再按价格 60%、交付 20%、质量 20% 排序"
        if candidates
        else "没有供应商同时满足数量、有效期和供货能力约束",
    }


def _smape(actual: list[float], forecast: list[float]) -> float:
    values = []
    for left, right in zip(actual, forecast, strict=True):
        denominator = (abs(left) + abs(right)) / 2
        if denominator:
            values.append(abs(left - right) / denominator)
    return round(mean(values) * 100, 2) if values else 0.0


def forecast_demand(
    db: Session,
    enterprise_id: str,
    product_id: str,
    as_of: date,
    *,
    forecast_start: date | None = None,
    forecast_end: date | None = None,
) -> dict[str, Any]:
    forecast_start = forecast_start or as_of + timedelta(days=1)
    forecast_end = forecast_end or forecast_start + timedelta(days=6)
    history = list(
        db.scalars(
            select(DemandHistory)
            .where(
                DemandHistory.enterprise_id == enterprise_id,
                DemandHistory.product_id == product_id,
                DemandHistory.period_start <= as_of,
            )
            .order_by(DemandHistory.period_start)
        )
    )
    values = [item.quantity for item in history]
    plan_quantity = float(
        db.scalar(
            select(func.coalesce(func.sum(ProductionPlan.planned_quantity), 0)).where(
                ProductionPlan.enterprise_id == enterprise_id,
                ProductionPlan.product_id == product_id,
                ProductionPlan.plan_date.between(forecast_start, forecast_end),
            )
        )
        or 0
    )
    plan_cutoff = db.scalar(
        select(func.max(ProductionPlan.plan_date)).where(
            ProductionPlan.enterprise_id == enterprise_id,
            ProductionPlan.product_id == product_id,
            ProductionPlan.plan_date.between(forecast_start, forecast_end),
        )
    )
    if not values:
        if plan_quantity > 0:
            margin = plan_quantity * 0.15
            return {
                "enterprise_id": enterprise_id,
                "product_id": product_id,
                "forecast_start": forecast_start.isoformat(),
                "forecast_end": forecast_end.isoformat(),
                "forecast_quantity": round(plan_quantity, 2),
                "lower_bound": round(max(0.0, plan_quantity - margin), 2),
                "upper_bound": round(plan_quantity + margin, 2),
                "method": "PRODUCTION_PLAN_FALLBACK",
                "method_note": "无历史用量，采用已确认生产计划并扩大预测区间",
                "data_points": 0,
                "data_cutoff": plan_cutoff.isoformat() if plan_cutoff else None,
                "mae": None,
                "smape": None,
                "historical_usage": 0.0,
                "production_plan_quantity": plan_quantity,
            }
        return {
            "enterprise_id": enterprise_id,
            "product_id": product_id,
            "forecast_start": forecast_start.isoformat(),
            "forecast_end": forecast_end.isoformat(),
            "forecast_quantity": None,
            "lower_bound": None,
            "upper_bound": None,
            "method": "INSUFFICIENT_DATA",
            "method_note": "无历史用量和生产计划，当前周期不生成预测数量",
            "data_points": 0,
            "data_cutoff": None,
            "mae": None,
            "smape": None,
            "historical_usage": 0.0,
            "production_plan_quantity": 0.0,
        }

    if len(values) >= 24:
        seasonal_period = 12
        recent = values[-seasonal_period:]
        previous = values[-2 * seasonal_period : -seasonal_period]
        trend = (mean(recent) - mean(previous)) / seasonal_period
        seasonal_ratio = recent[-1] / mean(recent) if mean(recent) else 1.0
        base = 0.65 * recent[-1] + 0.35 * mean(recent)
        forecast_value = max(0.0, (base + trend) * seasonal_ratio)
        fitted = [max(0.0, values[index - seasonal_period]) for index in range(seasonal_period, len(values))]
        actual = values[seasonal_period:]
        method = "SEASONAL_EXPONENTIAL_SMOOTHING"
    else:
        weights = [0.5, 0.3, 0.2][-min(3, len(values)) :]
        sample = values[-len(weights) :]
        divisor = sum(weights)
        forecast_value = sum(value * weight for value, weight in zip(sample, weights, strict=True)) / divisor
        fitted = [mean(values[max(0, index - 3) : index]) for index in range(1, len(values))]
        actual = values[1:]
        method = "WEIGHTED_MOVING_AVERAGE"

    residuals = [abs(left - right) for left, right in zip(actual, fitted, strict=True)]
    mae = mean(residuals) if residuals else 0.0
    if plan_quantity:
        forecast_value = forecast_value * 0.7 + plan_quantity * 0.3
    margin = max(mae * 1.64, forecast_value * 0.08)
    history_cutoff = max(item.period_start for item in history)
    data_cutoff = max(history_cutoff, plan_cutoff) if plan_cutoff else history_cutoff
    return {
        "enterprise_id": enterprise_id,
        "product_id": product_id,
        "forecast_start": forecast_start.isoformat(),
        "forecast_end": forecast_end.isoformat(),
        "forecast_quantity": round(forecast_value, 2),
        "lower_bound": round(max(0.0, forecast_value - margin), 2),
        "upper_bound": round(forecast_value + margin, 2),
        "method": method,
        "data_points": len(values),
        "data_cutoff": data_cutoff.isoformat(),
        "mae": round(mae, 2),
        "smape": _smape(actual, fitted),
        "historical_usage": round(mean(values[-min(3, len(values)) :]), 2),
        "production_plan_quantity": plan_quantity,
        "method_note": "历史充分时使用季节性平滑；样本不足时自动退化为加权移动平均",
    }
