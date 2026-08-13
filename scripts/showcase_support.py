from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from app.domain.algorithms import run_carpool, run_warehouse_pool
from app.domain.dashboard_catalogs import ShowcaseScenario
from app.shared.models import TransportOrder, Vehicle, Warehouse


def _stable_id(kind: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"black-soil-loop:e02:{kind}:{key}"))


def build_scenario_inputs(scenario: ShowcaseScenario):
    orders = [
        TransportOrder(
            id=_stable_id("order", item.order_key),
            order_no=f"E02-{item.order_key.upper()}",
            scenario_code=scenario.scenario_code,
            enterprise_id=_stable_id("enterprise", item.enterprise_name),
            product_id=_stable_id("product", item.product_name),
            store_id=_stable_id("store", item.store_name),
            origin_latitude=item.origin_latitude,
            origin_longitude=item.origin_longitude,
            destination_latitude=item.destination_latitude,
            destination_longitude=item.destination_longitude,
            departure_at=item.departure_at,
            warehouse_inbound_start=item.warehouse_inbound_start,
            warehouse_inbound_end=item.warehouse_inbound_end,
            warehouse_outbound_start=item.warehouse_outbound_start,
            warehouse_outbound_end=item.warehouse_outbound_end,
            quantity=item.quantity,
            unit=item.unit,
            weight_kg=item.weight_kg,
            volume_m3=item.volume_m3,
            temperature_zone=item.temperature_zone,
            status="DRAFT",
            object_version=1,
        )
        for item in scenario.orders
    ]
    vehicles = [
        Vehicle(
            id=_stable_id("vehicle", item.vehicle_key),
            driver_id=_stable_id("driver", item.vehicle_key),
            plate_no=f"演示-{item.vehicle_key}",
            temperature_zone=item.temperature_zone,
            max_weight_kg=item.max_weight_kg,
            max_volume_m3=item.max_volume_m3,
            enabled=True,
            object_version=1,
        )
        for item in scenario.vehicles
    ]
    warehouses = [
        Warehouse(
            id=_stable_id("warehouse", item.warehouse_key),
            enterprise_id=_stable_id("enterprise", f"warehouse-{item.warehouse_key}"),
            code=f"E02-{item.warehouse_key.upper()}",
            name=item.name,
            temperature_zone=item.temperature_zone,
            latitude=item.latitude,
            longitude=item.longitude,
            capacity_m3=item.capacity_m3,
            used_m3=item.used_m3,
            reserved_m3=item.reserved_m3,
            object_version=1,
        )
        for item in scenario.warehouses
    ]
    return orders, vehicles, warehouses


def calculate_scenario(scenario: ShowcaseScenario) -> tuple[dict, dict, dict]:
    orders, vehicles, warehouses = build_scenario_inputs(scenario)
    carpool = run_carpool(orders, vehicles)
    warehouse = run_warehouse_pool(orders, warehouses)
    mappings = {
        "private_order_keys": {item.id: source.order_key for item, source in zip(orders, scenario.orders, strict=True)},
        "private_vehicle_keys": {
            item.id: source.vehicle_key for item, source in zip(vehicles, scenario.vehicles, strict=True)
        },
        "private_warehouse_keys": {
            item.id: source.warehouse_key for item, source in zip(warehouses, scenario.warehouses, strict=True)
        },
    }
    return carpool, warehouse, mappings
