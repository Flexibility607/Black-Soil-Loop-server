from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.dashboard_map import build_public_map
from app.domain.dashboard_showcase import build_algorithm_showcase
from app.shared.config import get_settings
from app.shared.errors import BusinessError
from app.shared.models import (
    Alert,
    DashboardProjection,
    Enterprise,
    InventoryBalance,
    LocationPoint,
    Product,
    Store,
    StoreDailyReport,
    TaskStop,
    TelemetryPoint,
    TransportOrder,
    TransportTask,
    Vehicle,
    Warehouse,
    utcnow,
)
from app.shared.optimistic import atomic_versioned_update
from app.shared.periods import PERIOD_VALUES, period_window
from app.shared.security import as_utc


def _number(value: Decimal | float | int | None) -> float:
    return round(float(value or 0), 2)


def build_dashboard_snapshot(db: Session, period: str = "30d") -> dict[str, Any]:
    now = utcnow()
    window = period_window(period, now)
    reports = list(
        db.scalars(
            select(StoreDailyReport).where(
                StoreDailyReport.report_date >= window.start_date,
                StoreDailyReport.report_date <= window.end_date,
                StoreDailyReport.authorized_for_dashboard.is_(True),
            )
        )
    )
    stores = {item.id: item for item in db.scalars(select(Store))}
    enterprises = {item.id: item for item in db.scalars(select(Enterprise))}
    products = {item.id: item for item in db.scalars(select(Product))}
    vehicles = {item.id: item for item in db.scalars(select(Vehicle))}
    tasks = list(db.scalars(select(TransportTask).order_by(TransportTask.updated_at.desc())))
    stops = list(db.scalars(select(TaskStop)))
    latest_telemetry = list(db.scalars(select(TelemetryPoint).order_by(TelemetryPoint.sampled_at.desc()).limit(100)))
    latest_locations = list(db.scalars(select(LocationPoint).order_by(LocationPoint.recorded_at.desc()).limit(100)))
    alerts = list(db.scalars(select(Alert).order_by(Alert.opened_at.desc()).limit(100)))

    daily: dict[str, dict[str, float]] = defaultdict(lambda: {"sales_amount": 0.0, "order_count": 0})
    daily_channel: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"sales_amount": 0.0, "order_count": 0}
    )
    channel: dict[str, dict[str, float]] = defaultdict(lambda: {"sales_amount": 0.0, "order_count": 0})
    store_sales: dict[str, dict[str, float]] = defaultdict(lambda: {"sales_amount": 0.0, "order_count": 0})
    for report in reports:
        day_key = report.report_date.isoformat()
        sales = _number(report.sales_amount)
        daily[day_key]["sales_amount"] += sales
        daily[day_key]["order_count"] += report.order_count
        store = stores.get(report.store_id)
        if store:
            channel[store.channel]["sales_amount"] += sales
            channel[store.channel]["order_count"] += report.order_count
            daily_channel[(day_key, store.channel)]["sales_amount"] += sales
            daily_channel[(day_key, store.channel)]["order_count"] += report.order_count
            store_sales[store.id]["sales_amount"] += sales
            store_sales[store.id]["order_count"] += report.order_count

    total_sales = sum(item["sales_amount"] for item in channel.values())
    operation_order_count = int(sum(item["order_count"] for item in daily.values()))
    demand_by_enterprise: dict[str, float] = defaultdict(float)
    daily_demand_by_unit: dict[tuple[str, str, str], float] = defaultdict(float)
    preorder_by_channel: dict[str, int] = defaultdict(int)
    daily_preorder_by_channel: dict[tuple[str, str], int] = defaultdict(int)
    preorder_count = 0
    committed_order_ids = {order_id for task in tasks for order_id in task.source_order_ids}
    orders = list(
        db.scalars(
            select(TransportOrder).where(
                TransportOrder.created_at >= window.start_at,
                TransportOrder.created_at < window.end_at,
            )
        )
    )
    for order in orders:
        if order.status not in {"CONFIRMED", "COMPLETED"} and order.id not in committed_order_ids:
            continue
        preorder_count += 1
        demand_by_enterprise[order.enterprise_id] += order.weight_kg
        day_key = as_utc(order.departure_at).date().isoformat()
        order_store = stores.get(order.store_id)
        channel_name = order_store.channel if order_store else "UNKNOWN"
        preorder_by_channel[channel_name] += 1
        daily_preorder_by_channel[(day_key, channel_name)] += 1
        daily_demand_by_unit[(day_key, channel_name, order.unit)] += order.quantity

    transport_counts: dict[str, int] = defaultdict(int)
    for task in tasks:
        transport_counts[task.status] += 1

    inventory_rows = list(db.scalars(select(InventoryBalance)))
    inventory_by_product: dict[str, float] = defaultdict(float)
    low_stock_count = 0
    for item in inventory_rows:
        inventory_by_product[item.product_id] += item.quantity
        if item.quantity <= item.low_stock_threshold:
            low_stock_count += 1

    open_alerts = [item for item in alerts if item.status == "OPEN"]
    telemetry_by_task: dict[str, TelemetryPoint] = {}
    for point in latest_telemetry:
        telemetry_by_task.setdefault(point.task_id, point)
    location_by_task: dict[str, LocationPoint] = {}
    for location in latest_locations:
        location_by_task.setdefault(location.task_id, location)
    stops_by_task: dict[str, list[TaskStop]] = defaultdict(list)
    for stop in stops:
        stops_by_task[stop.task_id].append(stop)

    route_items = []
    for task in tasks:
        vehicle = vehicles.get(task.vehicle_id)
        point = telemetry_by_task.get(task.id)
        location = location_by_task.get(task.id)
        route_items.append(
            {
                "task_id": task.id,
                "task_no": task.task_no,
                "status": task.status,
                "plate_no": vehicle.plate_no if vehicle else None,
                "temperature_zone": task.temperature_zone,
                "origin": {"latitude": task.origin_latitude, "longitude": task.origin_longitude},
                "destination": {"latitude": task.destination_latitude, "longitude": task.destination_longitude},
                "stops": [
                    {
                        "store_id": stop.store_id,
                        "store_name": stores[stop.store_id].name if stop.store_id in stores else None,
                        "channel": stores[stop.store_id].channel if stop.store_id in stores else None,
                        "latitude": stop.latitude,
                        "longitude": stop.longitude,
                        "status": stop.status,
                    }
                    for stop in sorted(stops_by_task[task.id], key=lambda item: item.sequence_no)
                ],
                "latest_telemetry": {
                    "temperature_c": point.temperature_c,
                    "humidity_pct": point.humidity_pct,
                    "latitude": point.latitude,
                    "longitude": point.longitude,
                    "sampled_at": point.sampled_at.isoformat(),
                    "anomaly_code": point.anomaly_code,
                }
                if point
                else None,
                "latest_location": {
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "speed_mps": location.speed_mps,
                    "accuracy_m": location.accuracy_m,
                    "recorded_at": location.recorded_at.isoformat(),
                }
                if location
                else None,
                "route_label": "经纬度估算路线",
            }
        )

    third_space_sales = sum(
        values["sales_amount"] for store_id, values in store_sales.items() if stores[store_id].channel == "THIRD_SPACE"
    )
    warehouses = list(db.scalars(select(Warehouse)))
    cutoff_candidates = (
        [as_utc(report.updated_at) for report in reports]
        + [as_utc(store.updated_at) for store in stores.values()]
        + [as_utc(enterprise.updated_at) for enterprise in enterprises.values()]
        + [as_utc(product.updated_at) for product in products.values()]
        + [as_utc(vehicle.updated_at) for vehicle in vehicles.values()]
        + [as_utc(order.updated_at) for order in orders]
        + [as_utc(task.updated_at) for task in tasks]
        + [as_utc(stop.updated_at) for stop in stops]
        + [as_utc(item.updated_at) for item in inventory_rows]
        + [as_utc(item.updated_at) for item in warehouses]
        + [as_utc(item.updated_at) for item in alerts]
        + [as_utc(point.sampled_at) for point in latest_telemetry]
        + [as_utc(location.recorded_at) for location in latest_locations]
    )
    data_cutoff = max(cutoff_candidates) if cutoff_candidates else None
    settings = get_settings()
    legacy_map = {
        "stores": [
            {
                "id": item.id,
                "name": item.name,
                "channel": item.channel,
                "latitude": item.latitude,
                "longitude": item.longitude,
            }
            for item in stores.values()
        ],
        "routes": route_items,
    }
    dashboard_map = {**legacy_map, **build_public_map(db)} if settings.dashboard_map_mode == "changchun" else legacy_map
    showcase = (
        build_algorithm_showcase(db)
        if settings.algorithm_showcase_enabled
        else {
            "schema_version": "1.0",
            "status": "WAITING",
            "status_label": "功能暂未启用",
            "data_cutoff": None,
            "data_cutoff_note": "协同运算方案展示尚未启用",
            "panels": [],
        }
    )
    return {
        "period": period,
        "currency": "CNY",
        "sales_amount_unit": "yuan",
        "order_count_unit": "单",
        "summary": {
            "enterprise_count": len(enterprises),
            "store_count": len(stores),
            "third_space_count": sum(store.channel == "THIRD_SPACE" for store in stores.values()),
            "total_sales_amount": round(total_sales, 2),
            "preorder_count": preorder_count,
            "operation_order_count": operation_order_count,
            "third_space_sales_amount": round(third_space_sales, 2),
            "third_space_sales_share_pct": round(third_space_sales / total_sales * 100, 2) if total_sales else 0,
            "active_transport_count": sum(task.status not in {"COMPLETED", "CANCELLED"} for task in tasks),
            "open_alert_count": len(open_alerts),
            "low_stock_count": low_stock_count,
        },
        "charts": {
            "demand_by_enterprise": [
                {"enterprise_id": key, "name": enterprises[key].name, "quantity_kg": round(value, 2)}
                for key, value in demand_by_enterprise.items()
            ],
            "daily_demand_by_unit": [
                {"date": day, "channel": channel_name, "unit": unit, "quantity": round(quantity, 2)}
                for (day, channel_name, unit), quantity in sorted(daily_demand_by_unit.items())
            ],
            "preorders_by_channel": [
                {"channel": channel_name, "count": count} for channel_name, count in sorted(preorder_by_channel.items())
            ],
            "daily_preorders_by_channel": [
                {"date": day, "channel": channel_name, "count": count}
                for (day, channel_name), count in sorted(daily_preorder_by_channel.items())
            ],
            "daily_operations": [
                {"date": key, **{name: round(value, 2) for name, value in values.items()}}
                for key, values in sorted(daily.items())
            ],
            "daily_operations_by_channel": [
                {"date": day, "channel": channel_name, **{name: round(value, 2) for name, value in values.items()}}
                for (day, channel_name), values in sorted(daily_channel.items())
            ],
            "channel_mix": [
                {"channel": key, **{name: round(value, 2) for name, value in values.items()}}
                for key, values in channel.items()
            ],
            "third_space_ranking": sorted(
                [
                    {
                        "store_id": store_id,
                        "store_name": stores[store_id].name,
                        **{name: round(value, 2) for name, value in values.items()},
                    }
                    for store_id, values in store_sales.items()
                    if stores[store_id].channel == "THIRD_SPACE"
                ],
                key=lambda item: item["sales_amount"],
                reverse=True,
            ),
            "inventory_by_product": [
                {"product_id": key, "product_name": products[key].name, "quantity": round(value, 2)}
                for key, value in inventory_by_product.items()
            ],
            "warehouse_capacity": [
                {
                    "warehouse_id": item.id,
                    "warehouse_name": item.name,
                    "temperature_zone": item.temperature_zone,
                    "capacity_m3": item.capacity_m3,
                    "used_m3": item.used_m3,
                    "utilization_pct": round(item.used_m3 / item.capacity_m3 * 100, 2),
                }
                for item in warehouses
            ],
            "transport_status": [{"status": key, "count": value} for key, value in transport_counts.items()],
        },
        "map": dashboard_map,
        "algorithm_showcase": showcase,
        "alerts": [
            {
                "id": item.id,
                "task_id": item.task_id,
                "alert_type": item.alert_type,
                "status": item.status,
                "message": item.message,
                "opened_at": item.opened_at.isoformat(),
                "object_version": item.object_version,
            }
            for item in alerts
        ],
        "data_quality": {
            "reporting_store_count": len({report.store_id for report in reports}),
            "store_count": len(stores),
            "report_coverage_pct": round(
                len({report.store_id for report in reports}) / len(stores) * 100,
                2,
            )
            if stores
            else 0,
            "route_estimation": "HAVERSINE",
        },
        "data_cutoff": data_cutoff,
    }


def refresh_dashboard_projection(
    db: Session,
    period: str = "30d",
    *,
    commit: bool = True,
) -> dict[str, Any]:
    period_window(period)
    projection_id = f"current:{period}"
    last_conflict: BusinessError | None = None
    for _ in range(3):
        db.flush()
        snapshot = build_dashboard_snapshot(db, period)
        raw_cutoff = snapshot.pop("data_cutoff")
        cutoff = as_utc(raw_cutoff) if raw_cutoff is not None else None
        projection = db.scalar(
            select(DashboardProjection)
            .where(DashboardProjection.id == projection_id)
            .execution_options(populate_existing=True)
        )
        if projection is None:
            try:
                with db.begin_nested():
                    projection = DashboardProjection(id=projection_id, snapshot=snapshot, data_cutoff=cutoff)
                    db.add(projection)
                    db.flush()
            except IntegrityError:
                db.expire_all()
                continue
        else:
            try:
                atomic_versioned_update(
                    db,
                    DashboardProjection,
                    projection.id,
                    projection.object_version,
                    {"snapshot": snapshot, "data_cutoff": cutoff},
                )
            except BusinessError as exc:
                if exc.code != "VERSION_CONFLICT":
                    raise
                last_conflict = exc
                db.expire_all()
                continue
        if commit:
            db.commit()
        return {**snapshot, "data_cutoff": cutoff}
    if last_conflict is not None:
        raise last_conflict
    raise BusinessError("PROJECTION_CONFLICT", "看板投影正在刷新，请稍后重试", status_code=409)


def read_dashboard_projection(db: Session, period: str = "30d") -> dict[str, Any] | None:
    period_window(period)
    projection = db.get(DashboardProjection, f"current:{period}")
    if projection is None:
        return None
    cutoff = as_utc(projection.data_cutoff) if projection.data_cutoff is not None else None
    return {**projection.snapshot, "data_cutoff": cutoff}


def refresh_all_dashboard_projections(db: Session, *, commit: bool = True) -> None:
    for index, period in enumerate(PERIOD_VALUES):
        refresh_dashboard_projection(
            db,
            period,
            commit=commit and index == len(PERIOD_VALUES) - 1,
        )
