from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from statistics import mean
from typing import Any

from sqlalchemy import select
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
    "capacity_utilization_limit": 0.9,
}

WAREHOUSE_RULES = {"distance_max_km": 30.0, "capacity_utilization_limit": 0.9}


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


def run_carpool(orders: list[TransportOrder], vehicles: list[Vehicle]) -> dict[str, Any]:
    """Build deterministic multi-order groups and keep every rejection explainable."""
    available = sorted((order for order in orders if order.status == "DRAFT"), key=lambda item: item.departure_at)
    remaining = {order.id: order for order in available}
    candidates: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    while remaining:
        anchor = min(remaining.values(), key=lambda item: item.departure_at)
        group = [anchor]
        rejection_reasons: dict[str, list[str]] = {}
        for order in sorted(remaining.values(), key=lambda item: item.departure_at):
            if order.id == anchor.id:
                continue
            compatible, reasons = _carpool_compatible(anchor, order)
            if compatible:
                group.append(order)
            else:
                rejection_reasons[order.id] = reasons

        compatible_vehicles = [
            vehicle
            for vehicle in vehicles
            if vehicle.enabled and vehicle.driver_id and vehicle.temperature_zone == anchor.temperature_zone
        ]
        chosen: Vehicle | None = None
        final_group: list[TransportOrder] = []
        for vehicle in sorted(compatible_vehicles, key=lambda item: (item.max_weight_kg, item.max_volume_m3)):
            selected: list[TransportOrder] = []
            weight = 0.0
            volume = 0.0
            for order in group:
                if (
                    weight + order.weight_kg <= vehicle.max_weight_kg * CARPOOL_RULES["capacity_utilization_limit"]
                    and volume + order.volume_m3 <= vehicle.max_volume_m3 * CARPOOL_RULES["capacity_utilization_limit"]
                ):
                    selected.append(order)
                    weight += order.weight_kg
                    volume += order.volume_m3
            if len(selected) >= 2 and len(selected) > len(final_group):
                chosen = vehicle
                final_group = selected

        if chosen and len(final_group) >= 2:
            total_weight = sum(order.weight_kg for order in final_group)
            total_volume = sum(order.volume_m3 for order in final_group)
            unique_enterprises = len({order.enterprise_id for order in final_group})
            unique_products = len({order.product_id for order in final_group})
            stops_by_store: dict[str, dict[str, Any]] = {}
            for order in final_group:
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
            stops = list(stops_by_store.values())
            candidates.append(
                {
                    "order_ids": [order.id for order in final_group],
                    "order_nos": [order.order_no for order in final_group],
                    "vehicle_id": chosen.id,
                    "driver_id": chosen.driver_id,
                    "plate_no": chosen.plate_no,
                    "temperature_zone": anchor.temperature_zone,
                    "planned_departure_at": min(order.departure_at for order in final_group).isoformat(),
                    "origin": {"latitude": anchor.origin_latitude, "longitude": anchor.origin_longitude},
                    "destination": {
                        "latitude": anchor.destination_latitude,
                        "longitude": anchor.destination_longitude,
                    },
                    "stops": stops,
                    "total_weight_kg": round(total_weight, 2),
                    "total_volume_m3": round(total_volume, 3),
                    "weight_utilization_pct": round(total_weight / chosen.max_weight_kg * 100, 1),
                    "volume_utilization_pct": round(total_volume / chosen.max_volume_m3 * 100, 1),
                    "enterprise_count": unique_enterprises,
                    "product_count": unique_products,
                    "route_label": "经纬度估算路线",
                    "explanation": "始发地、目的地、发车时间、温区和车辆容量均满足规则",
                }
            )
            for order in final_group:
                remaining.pop(order.id, None)
        else:
            reason = "没有可承载至少两单的同温区车辆"
            if not compatible_vehicles:
                reason = "没有可用的同温区车辆和司机"
            elif len(group) == 1 and rejection_reasons:
                first = next(iter(rejection_reasons.values()))
                reason = "；".join(first)
            unmatched.append({"order_id": anchor.id, "order_no": anchor.order_no, "reason": reason})
            remaining.pop(anchor.id, None)

    return {
        "rules_version": "carpool-balanced-v1",
        "rules": CARPOOL_RULES,
        "candidates": candidates,
        "unmatched": unmatched,
    }


def run_warehouse_pool(orders: list[TransportOrder], warehouses: list[Warehouse]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[TransportOrder]] = defaultdict(list)
    for order in orders:
        if order.status == "DRAFT":
            grouped[(order.scenario_code, order.temperature_zone)].append(order)

    for (scenario_code, temperature_zone), group in sorted(grouped.items()):
        volume = sum(order.volume_m3 for order in group)
        center_lat = mean(order.origin_latitude for order in group)
        center_lon = mean(order.origin_longitude for order in group)
        ranked: list[dict[str, Any]] = []
        for warehouse in warehouses:
            if warehouse.temperature_zone != temperature_zone:
                continue
            distance = haversine_km(center_lat, center_lon, warehouse.latitude, warehouse.longitude)
            available = warehouse.capacity_m3 - warehouse.used_m3
            eligible = (
                distance <= WAREHOUSE_RULES["distance_max_km"]
                and volume <= available * WAREHOUSE_RULES["capacity_utilization_limit"]
            )
            ranked.append(
                {
                    "warehouse_id": warehouse.id,
                    "warehouse_name": warehouse.name,
                    "estimated_distance_km": round(distance, 2),
                    "available_volume_m3": round(available, 2),
                    "eligible": eligible,
                    "reason": "满足距离、温区和库容规则" if eligible else "距离超过 30 km 或合并后超过可用库容 90%",
                }
            )
        ranked.sort(key=lambda item: (not item["eligible"], item["estimated_distance_km"]))
        item = {
            "scenario_code": scenario_code,
            "temperature_zone": temperature_zone,
            "order_ids": [order.id for order in group],
            "enterprise_count": len({order.enterprise_id for order in group}),
            "product_count": len({order.product_id for order in group}),
            "required_volume_m3": round(volume, 2),
            "candidate_warehouses": ranked,
            "route_label": "经纬度估算路线",
        }
        if ranked and ranked[0]["eligible"]:
            candidates.append(item)
        else:
            unmatched.append({**item, "reason": "没有同时满足温区、距离和库容的仓库"})
    return {
        "rules_version": "warehouse-balanced-v1",
        "rules": WAREHOUSE_RULES,
        "candidates": candidates,
        "unmatched": unmatched,
    }


def run_procurement(db: Session, product_id: str, required_quantity: float, as_of: date) -> dict[str, Any]:
    product = db.get(Product, product_id)
    tiers = list(
        db.scalars(
            select(SupplierPriceTier).where(
                SupplierPriceTier.product_id == product_id,
                SupplierPriceTier.valid_from <= as_of,
                SupplierPriceTier.valid_to >= as_of,
                SupplierPriceTier.min_quantity <= required_quantity,
                SupplierPriceTier.supply_capacity >= required_quantity,
            )
        )
    )
    candidates: list[dict[str, Any]] = []
    prices = [
        float(tier.unit_price) for tier in tiers if tier.max_quantity is None or required_quantity <= tier.max_quantity
    ]
    if prices:
        low, high = min(prices), max(prices)
        for tier in tiers:
            if tier.max_quantity is not None and required_quantity > tier.max_quantity:
                continue
            supplier = db.get(Supplier, tier.supplier_id)
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
                    "supply_capacity": tier.supply_capacity,
                }
            )
    candidates.sort(key=lambda item: (-item["composite_score"], item["total_amount"]))
    return {
        "rules_version": "procurement-value-v1",
        "product_id": product_id,
        "product_name": product.name if product else None,
        "required_quantity": required_quantity,
        "weights": {"price": 0.6, "delivery": 0.2, "quality": 0.2},
        "candidates": candidates,
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


def forecast_demand(db: Session, enterprise_id: str, product_id: str, as_of: date) -> dict[str, Any]:
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
    if not values:
        return {
            "enterprise_id": enterprise_id,
            "product_id": product_id,
            "forecast_quantity": 0,
            "lower_bound": 0,
            "upper_bound": 0,
            "method": "NO_DATA",
            "data_cutoff": as_of.isoformat(),
            "mae": None,
            "smape": None,
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
    plan_quantity = db.scalar(
        select(ProductionPlan.planned_quantity).where(
            ProductionPlan.enterprise_id == enterprise_id,
            ProductionPlan.product_id == product_id,
            ProductionPlan.plan_date > as_of,
        )
    )
    if plan_quantity:
        forecast_value = forecast_value * 0.7 + float(plan_quantity) * 0.3
    margin = max(mae * 1.64, forecast_value * 0.08)
    return {
        "enterprise_id": enterprise_id,
        "product_id": product_id,
        "forecast_quantity": round(forecast_value, 2),
        "lower_bound": round(max(0.0, forecast_value - margin), 2),
        "upper_bound": round(forecast_value + margin, 2),
        "method": method,
        "data_points": len(values),
        "data_cutoff": max(item.period_start for item in history).isoformat(),
        "mae": round(mae, 2),
        "smape": _smape(actual, fitted),
        "production_plan_quantity": float(plan_quantity) if plan_quantity else None,
        "method_note": "历史充分时使用季节性平滑；样本不足时自动退化为加权移动平均",
    }
