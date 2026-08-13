from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.dashboard_catalogs import load_map_catalog
from app.shared.config import get_settings
from app.shared.dictionaries import ALERT_TYPE_LABELS, TRANSPORT_STATUS_LABELS, enum_label
from app.shared.models import (
    Alert,
    DashboardMapPoint,
    LocationPoint,
    Store,
    TaskStop,
    TelemetryPoint,
    TransportTask,
    Vehicle,
    utcnow,
)
from app.shared.security import as_utc

ACTIVE_TASK_STATUSES = {"PUBLISHED", "DRIVER_ACCEPTED", "PICKED_UP", "IN_TRANSIT"}


def _finite_in_bounds(longitude: float, latitude: float, bounds) -> bool:
    return math.isfinite(longitude) and math.isfinite(latitude) and bounds.contains(longitude, latitude)


def _masked_plate(value: str | None) -> str:
    if not value:
        return "配送车辆"
    if len(value) <= 3:
        return f"{value[0]}***"
    return f"{value[:2]}·{value[2:4]}***"


def _location_state(recorded_at) -> str:
    settings = get_settings()
    now = utcnow()
    age = now - as_utc(recorded_at)
    if age < timedelta(minutes=-5):
        return "INVALID"
    if age <= timedelta(minutes=settings.map_location_delayed_minutes):
        return "FRESH"
    if age <= timedelta(minutes=settings.map_location_stale_minutes):
        return "DELAYED"
    return "STALE"


def build_public_map(db: Session) -> dict:
    catalog = load_map_catalog()
    bounds = catalog.bounds
    catalog_rows = list(
        db.scalars(
            select(DashboardMapPoint)
            .where(DashboardMapPoint.enabled.is_(True), DashboardMapPoint.featured.is_(True))
            .order_by(DashboardMapPoint.display_order, DashboardMapPoint.point_code)
        )
    )
    points = [
        {
            "point_code": row.point_code,
            "display_name": row.display_name,
            "point_type": row.point_type,
            "brand_name": row.brand_name,
            "address": row.address,
            "longitude": row.longitude,
            "latitude": row.latitude,
            "coordinate_accuracy": row.coordinate_accuracy,
            "featured": row.featured,
            "display_order": row.display_order,
            "is_demo": row.source_type == "DEMO_SIMULATION",
            "coordinate_note": (
                "长春市区演示坐标，仅用于大屏场景展示，不提供导航。" if row.source_type == "DEMO_SIMULATION" else None
            ),
        }
        for row in catalog_rows
    ]

    tasks = list(
        db.scalars(
            select(TransportTask)
            .where(TransportTask.status.in_(ACTIVE_TASK_STATUSES))
            .order_by(TransportTask.updated_at.desc(), TransportTask.id)
        )
    )
    task_ids = [task.id for task in tasks]
    stops_by_task: dict[str, list[TaskStop]] = defaultdict(list)
    locations: dict[str, LocationPoint] = {}
    telemetry: dict[str, TelemetryPoint] = {}
    alerts_by_task: dict[str, list[Alert]] = defaultdict(list)
    if task_ids:
        for stop in db.scalars(
            select(TaskStop)
            .where(TaskStop.task_id.in_(task_ids))
            .order_by(TaskStop.task_id, TaskStop.sequence_no, TaskStop.id)
        ):
            stops_by_task[stop.task_id].append(stop)
        for item in db.scalars(
            select(LocationPoint)
            .where(LocationPoint.task_id.in_(task_ids))
            .order_by(LocationPoint.task_id, LocationPoint.recorded_at.desc(), LocationPoint.id.desc())
        ):
            locations.setdefault(item.task_id, item)
        for item in db.scalars(
            select(TelemetryPoint)
            .where(TelemetryPoint.task_id.in_(task_ids))
            .order_by(TelemetryPoint.task_id, TelemetryPoint.sampled_at.desc(), TelemetryPoint.id.desc())
        ):
            telemetry.setdefault(item.task_id, item)
        for item in db.scalars(
            select(Alert)
            .where(Alert.task_id.in_(task_ids), Alert.status.in_(("OPEN", "ACKNOWLEDGED")))
            .order_by(Alert.task_id, Alert.opened_at.desc(), Alert.id.desc())
        ):
            alerts_by_task[item.task_id].append(item)

    stores = {item.id: item for item in db.scalars(select(Store))}
    vehicles = {item.id: item for item in db.scalars(select(Vehicle))}
    excluded: dict[str, int] = defaultdict(int)
    routes = []
    for task in tasks:
        next_stop = next((item for item in stops_by_task[task.id] if item.status == "PLANNED"), None)
        if next_stop is None:
            excluded["NO_NEXT_STOP"] += 1
            continue
        if not _finite_in_bounds(next_stop.longitude, next_stop.latitude, bounds):
            excluded["TARGET_OUT_OF_SCOPE"] += 1
            continue
        location = locations.get(task.id)
        freshness = _location_state(location.recorded_at) if location else "STALE"
        use_location = bool(
            location
            and freshness in {"FRESH", "DELAYED"}
            and _finite_in_bounds(location.longitude, location.latitude, bounds)
        )
        if use_location:
            origin = {
                "kind": "VEHICLE",
                "longitude": location.longitude,
                "latitude": location.latitude,
            }
        elif _finite_in_bounds(task.origin_longitude, task.origin_latitude, bounds):
            origin = {
                "kind": "ORIGIN",
                "longitude": task.origin_longitude,
                "latitude": task.origin_latitude,
            }
        else:
            excluded["ORIGIN_OUT_OF_SCOPE"] += 1
            continue
        store = stores.get(next_stop.store_id)
        route_alerts = alerts_by_task[task.id]
        sample = telemetry.get(task.id)
        vehicle = vehicles.get(task.vehicle_id)
        routes.append(
            {
                "route_key": hashlib.sha256(f"public-map:{task.id}".encode()).hexdigest()[:16],
                "status": task.status,
                "status_label": enum_label(TRANSPORT_STATUS_LABELS, task.status),
                "vehicle_label": _masked_plate(vehicle.plate_no if vehicle else None),
                "from": origin,
                "to": {
                    "point_code": None,
                    "display_name": store.name if store else "运输目标点",
                    "channel": store.channel if store else "UNKNOWN",
                    "longitude": next_stop.longitude,
                    "latitude": next_stop.latitude,
                },
                "latest_location": {
                    "longitude": location.longitude,
                    "latitude": location.latitude,
                    "speed_mps": location.speed_mps,
                    "accuracy_m": location.accuracy_m,
                    "recorded_at": as_utc(location.recorded_at).isoformat(),
                    "freshness": freshness,
                }
                if location
                else None,
                "latest_telemetry": {
                    "temperature_c": sample.temperature_c,
                    "humidity_pct": sample.humidity_pct,
                    "sampled_at": as_utc(sample.sampled_at).isoformat(),
                    "anomaly_code": sample.anomaly_code,
                }
                if sample
                else None,
                "alerts": [
                    {
                        "alert_type": alert.alert_type,
                        "alert_type_label": enum_label(ALERT_TYPE_LABELS, alert.alert_type),
                        "message": alert.message,
                        "opened_at": as_utc(alert.opened_at).isoformat(),
                    }
                    for alert in route_alerts[:3]
                ],
                "alert_count": len(route_alerts),
                "render_state": "IN_SCOPE",
                "fallback": not use_location,
                "route_label": "经纬度估算路线",
            }
        )
    return {
        "schema_version": "2.0",
        "catalog_version": catalog.catalog_version,
        "crs": "EPSG:4326",
        "coordinate_order": "longitude,latitude",
        "scope": {
            "name": "长春市及近郊",
            "map_name": "changchun-service-area",
            "asset": catalog.geojson_asset,
            "bounds": bounds.as_list(),
            "route_method": "HAVERSINE_STRAIGHT_LINE",
            "disclaimer": "经纬度估算路线，不代表道路导航",
            "attribution": "© OpenStreetMap contributors，ODbL 1.0",
        },
        "points": points,
        "active_routes": routes,
        "excluded_route_summary": {"count": sum(excluded.values()), "reasons": dict(sorted(excluded.items()))},
    }
