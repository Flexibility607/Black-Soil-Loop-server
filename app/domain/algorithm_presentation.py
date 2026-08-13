from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.dashboard_showcase import assert_public_showcase_safe
from app.shared.dictionaries import TEMPERATURE_ZONE_LABELS, enum_label
from app.shared.models import Product, Store, TransportOrder, Vehicle, Warehouse


def _reasons(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(item.get("reason") or "未形成匹配方案")[:240] for item in items)
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]


def _common_input(orders: list[TransportOrder]) -> dict[str, Any]:
    return {
        "enterprise_count": len({item.enterprise_id for item in orders}),
        "order_count": len(orders),
        "product_count": len({item.product_id for item in orders}),
        "temperature_zones": [
            enum_label(TEMPERATURE_ZONE_LABELS, zone)
            for zone in ("AMBIENT", "CHILLED", "FROZEN")
            if any(item.temperature_zone == zone for item in orders)
        ],
    }


def carpool_presentation(
    db: Session,
    orders: list[TransportOrder],
    vehicles: list[Vehicle],
    result: dict[str, Any],
) -> dict[str, Any]:
    order_lookup = {item.id: item for item in orders}
    stores = {item.id: item.name for item in db.scalars(select(Store))}
    products = {item.id: item.name for item in db.scalars(select(Product))}
    vehicle_lookup = {item.id: item for item in vehicles}
    allocations: list[dict[str, Any]] = []
    allocated_orders = 0
    for candidate in result.get("candidates", []):
        selected = [order_lookup[item] for item in candidate.get("order_ids", []) if item in order_lookup]
        vehicle = vehicle_lookup.get(candidate.get("vehicle_id"))
        allocations.append(
            {
                "vehicle_label": (
                    f"{enum_label(TEMPERATURE_ZONE_LABELS, candidate.get('temperature_zone'))}配送车"
                    f"（{round(vehicle.max_weight_kg / 1000):g} 吨级）"
                    if vehicle
                    else "配送车辆"
                ),
                "temperature_zone_label": enum_label(
                    TEMPERATURE_ZONE_LABELS, candidate.get("temperature_zone")
                ),
                "enterprise_count": candidate.get("enterprise_count", 0),
                "order_count": len(selected),
                "product_count": len({products.get(item.product_id, "名称待补充") for item in selected}),
                "store_names": list(dict.fromkeys(stores.get(item.store_id, "名称待补充") for item in selected)),
                "departure_start": min((item.departure_at for item in selected), default=None),
                "departure_end": max((item.departure_at for item in selected), default=None),
                "total_weight_kg": candidate.get("total_weight_kg", 0),
                "total_volume_m3": candidate.get("total_volume_m3", 0),
                "weight_utilization_pct": candidate.get("weight_utilization_pct", 0),
                "volume_utilization_pct": candidate.get("volume_utilization_pct", 0),
                "capacity_utilization_pct": candidate.get("capacity_utilization_pct", 0),
                "detour_estimated_km": candidate.get("detour_estimated_km", 0),
                "mileage_benefit_estimated_km": candidate.get("mileage_benefit_estimated_km", 0),
                "route_label": "经纬度估算路线",
                "recommendation_label": "首选方案" if not allocations else "备选方案",
                "explanation": candidate.get("explanation") or "满足当前拼车约束",
            }
        )
        allocated_orders += len(selected)
    unmatched = result.get("unmatched", [])
    status = "READY" if allocations and not unmatched else "PARTIAL" if allocations else "NO_MATCH"
    presentation = {
        "title": "拼车联配测算结果",
        "status": status,
        "status_label": {
            "READY": "已形成可执行方案",
            "PARTIAL": "已形成部分方案，仍有待调整项",
            "NO_MATCH": "本次测算未形成可执行方案",
        }[status],
        "headline": (
            f"形成 {len(allocations)} 个配送组，{allocated_orders} 单已分配，{len(unmatched)} 单待调整"
            if allocations
            else "本次测算未形成可执行方案"
        ),
        "input_summary": _common_input(orders),
        "allocations": allocations,
        "unmatched_reasons": _reasons(unmatched),
    }
    assert_public_showcase_safe(presentation, "presentation")
    return presentation


def warehouse_presentation(
    orders: list[TransportOrder],
    warehouses: list[Warehouse],
    result: dict[str, Any],
) -> dict[str, Any]:
    warehouse_names = {item.id: item.name for item in warehouses}
    allocations: list[dict[str, Any]] = []
    for group in result.get("candidates", []):
        for index, candidate in enumerate(group.get("candidate_warehouses", [])):
            allocations.append(
                {
                    "warehouse_name": warehouse_names.get(candidate.get("warehouse_id"), "名称待补充"),
                    "temperature_zone_label": enum_label(
                        TEMPERATURE_ZONE_LABELS, group.get("temperature_zone")
                    ),
                    "enterprise_count": group.get("enterprise_count", 0),
                    "order_count": len(group.get("order_ids", [])),
                    "product_count": group.get("product_count", 0),
                    "required_volume_m3": group.get("required_volume_m3", 0),
                    "available_volume_m3": candidate.get("available_volume_m3", 0),
                    "remaining_volume_m3": candidate.get("remaining_volume_m3", 0),
                    "estimated_distance_km": candidate.get("estimated_distance_km", 0),
                    "inbound_window": group.get("inbound_window"),
                    "outbound_window": group.get("outbound_window"),
                    "eligible": bool(candidate.get("eligible")),
                    "recommendation_label": (
                        "推荐仓库" if index == 0 and candidate.get("eligible") else "备选仓库"
                    ),
                    "reasons": candidate.get("reasons") or [candidate.get("reason") or "满足当前匹配规则"],
                    "route_label": "经纬度估算距离",
                }
            )
    unmatched = result.get("unmatched", [])
    status = "READY" if allocations and not unmatched else "PARTIAL" if allocations else "NO_MATCH"
    presentation = {
        "title": "共享仓匹配测算结果",
        "status": status,
        "status_label": {
            "READY": "已形成可执行方案",
            "PARTIAL": "已形成部分方案，仍有待调整项",
            "NO_MATCH": "本次测算未形成可执行方案",
        }[status],
        "headline": f"形成 {len(allocations)} 个仓库匹配结果" if allocations else "本次测算未形成可执行方案",
        "input_summary": _common_input(orders),
        "allocations": allocations[:24],
        "unmatched_reasons": _reasons(unmatched),
    }
    assert_public_showcase_safe(presentation, "presentation")
    return presentation
