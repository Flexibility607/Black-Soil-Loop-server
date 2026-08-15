from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.domain.dashboard_catalogs import catalog_sha256, load_map_catalog, load_showcase_catalog
from app.domain.forecasts import generate_next_week_forecasts, next_week_window
from app.domain.procurement import generate_procurement_aggregations, procurement_cycle
from app.shared.config import get_settings
from app.shared.database import Base
from app.shared.models import (
    Alert,
    AlgorithmRun,
    Attachment,
    AuditLog,
    DashboardMapPoint,
    DashboardProjection,
    DeadLetter,
    DemandHistory,
    DemoCaseInstallation,
    Driver,
    Enterprise,
    InventoryBalance,
    InventoryMovement,
    InventoryMovementProjection,
    LocationPoint,
    OutboxEvent,
    ProcurementDemandConfirmation,
    Product,
    ProductionPlan,
    Receipt,
    StockoutRequest,
    Store,
    StoreDailyReport,
    Supplier,
    SupplierPriceTier,
    TaskEvent,
    TaskException,
    TaskStop,
    TelemetryIssue,
    TelemetryPoint,
    TransportOrder,
    TransportPlan,
    TransportTask,
    User,
    UserSession,
    Vehicle,
    Warehouse,
    WarehousePoolPlan,
    WarehouseReservation,
    utcnow,
)
from app.shared.periods import SHANGHAI
from app.shared.security import hash_password
from scripts.showcase_support import calculate_scenario

CONTENT_ROOT = Path(__file__).resolve().parents[1] / "content"
FIXED_DEMO_CATALOG_PATH = CONTENT_ROOT / "fixed-demo-case.v1.json"
DEMO_ATTACHMENTS_ROOT = CONTENT_ROOT / "demo-attachments"
CASE_NAMESPACE = uuid5(NAMESPACE_URL, "black-soil-loop:fixed-demo-case")
PRESERVED_TABLES = {
    DemoCaseInstallation.__table__.key,
    DashboardProjection.__table__.key,
    AuditLog.__table__.key,
    OutboxEvent.__table__.key,
    DeadLetter.__table__.key,
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccountItem(StrictModel):
    username: str = Field(pattern=r"^[a-z0-9-]{4,40}$")
    display_name: str = Field(min_length=2, max_length=120)
    role: Literal["park_admin", "enterprise_admin", "driver", "store_manager", "third_space_manager"]
    enterprise_code: str | None = None
    store_code: str | None = None
    driver_code: str | None = None


class EnterpriseItem(StrictModel):
    code: str
    name: str
    enabled: bool


class StoreItem(StrictModel):
    code: str
    enterprise_code: str
    name: str
    channel: Literal["THIRD_SPACE", "TRADITIONAL"]
    longitude: float
    latitude: float
    weight: float = Field(gt=0)


class ProductItem(StrictModel):
    code: str
    name: str
    category: str
    unit: str
    temperature_zone: Literal["AMBIENT", "CHILLED", "FROZEN"]


class WarehouseItem(StrictModel):
    code: str
    enterprise_code: str
    name: str
    temperature_zone: Literal["AMBIENT", "CHILLED", "FROZEN"]
    longitude: float
    latitude: float
    capacity_m3: float = Field(gt=0)
    used_m3: float = Field(ge=0)
    reserved_m3: float = Field(ge=0)


class SupplierItem(StrictModel):
    code: str
    name: str
    delivery_score: float = Field(ge=0, le=100)
    quality_score: float = Field(ge=0, le=100)


class FleetItem(StrictModel):
    driver_code: str
    driver_name: str
    vehicle_label: str
    temperature_zone: Literal["AMBIENT", "CHILLED", "FROZEN"]
    max_weight_kg: float = Field(gt=0)
    max_volume_m3: float = Field(gt=0)
    enabled: bool


class AttachmentItem(StrictModel):
    purpose: Literal["DELIVERY_HANDOVER", "RECEIPT_PROOF"]
    file_name: str = Field(pattern=r"^[a-z0-9-]+\.txt$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SupplierPricingBlueprint(StrictModel):
    tier_minimums: list[float] = Field(min_length=2, max_length=4)
    first_tier_maximum: float = Field(gt=0)
    base_unit_price: float = Field(gt=0)
    product_price_step: float = Field(ge=0)
    supplier_price_step: float = Field(ge=0)
    tier_discount_step: float = Field(ge=0)
    capacity_base: float = Field(gt=0)
    capacity_step: float = Field(ge=0)
    valid_days_before: int = Field(ge=0, le=365)
    valid_days_after: int = Field(ge=1, le=730)


class RollingBusinessBlueprint(StrictModel):
    days: Literal[31]
    origin_longitude: float
    origin_latitude: float
    quantity_bases: list[float] = Field(min_length=2, max_length=2)
    weight_bases_kg: list[float] = Field(min_length=2, max_length=2)
    volume_bases_m3: list[float] = Field(min_length=2, max_length=2)
    recent_confirmed_days: int = Field(ge=1, le=7)
    third_space_sales_base: float = Field(gt=0)
    traditional_sales_base: float = Field(gt=0)
    average_order_value: float = Field(gt=0)
    minimum_daily_orders: int = Field(ge=1)


class InventoryBlueprint(StrictModel):
    product_count: int = Field(ge=1, le=20)
    threshold_base: float = Field(ge=0)
    threshold_step: float = Field(ge=0)
    low_stock_count: int = Field(ge=1)
    critical_stock_count: int = Field(ge=1)
    low_stock_delta: float = Field(gt=0)
    healthy_quantity_base: float = Field(gt=0)
    healthy_store_step: float = Field(ge=0)
    healthy_product_step: float = Field(ge=0)
    stockout_statuses: list[Literal["SUBMITTED", "PROCESSING", "FULFILLED", "CANCELLED"]]
    stockout_quantity_base: float = Field(gt=0)
    stockout_quantity_step: float = Field(ge=0)
    manual_movement_deltas: list[float] = Field(min_length=2)


class FulfillmentBlueprint(StrictModel):
    task_statuses: list[
        Literal[
            "PUBLISHED",
            "DRIVER_ACCEPTED",
            "PICKED_UP",
            "IN_TRANSIT",
            "DELIVERED",
            "COMPLETED",
            "CANCELLED",
        ]
    ] = Field(min_length=8, max_length=8)
    task_weight_base_kg: float = Field(gt=0)
    task_weight_step_kg: float = Field(ge=0)
    task_volume_base_m3: float = Field(gt=0)
    task_volume_step_m3: float = Field(ge=0)
    location_age_minutes: list[int] = Field(min_length=5, max_length=5)
    alert_states: list[Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]] = Field(min_length=4)
    alert_types: list[Literal["HIGH_TEMPERATURE", "HIGH_HUMIDITY"]] = Field(min_length=4)
    telemetry_issue_types: list[
        Literal["TELEMETRY_ANOMALY", "TASK_STATUS_MISMATCH", "VEHICLE_MISMATCH", "DRIVER_MISMATCH"]
    ] = Field(min_length=4, max_length=4)
    receipt_statuses: list[Literal["FULL", "PARTIAL", "REJECTED"]] = Field(min_length=6, max_length=6)


class PlanningBlueprint(StrictModel):
    forecast_enterprise_count: int = Field(ge=1, le=12)
    product_count: int = Field(ge=1, le=20)
    history_points_by_product: list[int] = Field(min_length=1)
    procurement_quantity_base: float = Field(gt=0)
    procurement_quantity_step: float = Field(ge=0)
    unit_warning_product_index: int = Field(ge=0)
    confirmed_aggregation_count: int = Field(ge=0)
    adjusted_aggregation_index: int = Field(ge=0)
    adjustment_factor: float = Field(gt=0, le=1)
    transport_plan_statuses: list[
        Literal["PUBLISHED", "CONFIRMED", "READY", "COMPLETED", "CANCELLED"]
    ] = Field(min_length=6, max_length=6)
    warehouse_plan_statuses: list[
        Literal["RESERVATION_REQUESTED", "RESERVED", "OCCUPIED", "RELEASED", "EXPIRED"]
    ] = Field(min_length=5, max_length=5)
    reservation_statuses: list[
        Literal["RESERVED", "OCCUPIED", "RELEASED", "EXPIRED"]
    ] = Field(min_length=5, max_length=5)


class GenerationBlueprint(StrictModel):
    generator_version: Literal["2026-08-15.1"]
    supplier_pricing: SupplierPricingBlueprint
    rolling_business: RollingBusinessBlueprint
    inventory: InventoryBlueprint
    fulfillment: FulfillmentBlueprint
    planning: PlanningBlueprint


class FixedDemoCatalog(StrictModel):
    case_key: Literal["changchun-fixed-showcase-v1"]
    case_name: str
    catalog_version: Literal["2026-08-15.1"]
    service_scope: Literal["长春市及市郊"]
    accounts: list[AccountItem] = Field(min_length=5, max_length=10)
    enterprises: list[EnterpriseItem] = Field(min_length=6, max_length=12)
    stores: list[StoreItem] = Field(min_length=12, max_length=30)
    products: list[ProductItem] = Field(min_length=8, max_length=20)
    warehouses: list[WarehouseItem] = Field(min_length=4, max_length=12)
    suppliers: list[SupplierItem] = Field(min_length=4, max_length=12)
    fleet: list[FleetItem] = Field(min_length=6, max_length=12)
    daily_curve: list[float] = Field(min_length=31, max_length=31)
    generation: GenerationBlueprint
    attachments: list[AttachmentItem] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def validate_references(self):
        collections = {
            "enterprise": [item.code for item in self.enterprises],
            "store": [item.code for item in self.stores],
            "product": [item.code for item in self.products],
            "warehouse": [item.code for item in self.warehouses],
            "supplier": [item.code for item in self.suppliers],
            "driver": [item.driver_code for item in self.fleet],
            "account": [item.username for item in self.accounts],
        }
        for name, values in collections.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{name} logical keys must be unique")
        enterprise_codes = set(collections["enterprise"])
        store_codes = set(collections["store"])
        driver_codes = set(collections["driver"])
        if any(item.enterprise_code not in enterprise_codes for item in self.stores):
            raise ValueError("store enterprise reference is invalid")
        if any(item.enterprise_code not in enterprise_codes for item in self.warehouses):
            raise ValueError("warehouse enterprise reference is invalid")
        for account in self.accounts:
            if account.enterprise_code and account.enterprise_code not in enterprise_codes:
                raise ValueError("account enterprise reference is invalid")
            if account.store_code and account.store_code not in store_codes:
                raise ValueError("account store reference is invalid")
            if account.driver_code and account.driver_code not in driver_codes:
                raise ValueError("account driver reference is invalid")
        if sum(item.channel == "THIRD_SPACE" for item in self.stores) != 6:
            raise ValueError("fixed case requires exactly six third-space stores")
        if {item.temperature_zone for item in self.products} != {"AMBIENT", "CHILLED", "FROZEN"}:
            raise ValueError("all temperature zones must be represented")
        if min(self.daily_curve) <= 0:
            raise ValueError("daily curve values must be positive")
        if self.generation.inventory.product_count > len(self.products):
            raise ValueError("inventory product_count exceeds the product catalog")
        if self.generation.planning.product_count > len(self.products):
            raise ValueError("planning product_count exceeds the product catalog")
        if len(self.generation.planning.history_points_by_product) != self.generation.planning.product_count:
            raise ValueError("history_points_by_product must match planning product_count")
        if len(self.generation.fulfillment.alert_states) != len(self.generation.fulfillment.alert_types):
            raise ValueError("alert state and type blueprints must align")
        planning = self.generation.planning
        if planning.unit_warning_product_index >= planning.product_count:
            raise ValueError("unit_warning_product_index exceeds planning product_count")
        if planning.adjusted_aggregation_index >= planning.product_count:
            raise ValueError("adjusted_aggregation_index exceeds planning product_count")
        if planning.confirmed_aggregation_count > planning.product_count:
            raise ValueError("confirmed_aggregation_count exceeds planning product_count")
        if len(self.generation.inventory.stockout_statuses) > len(self.stores):
            raise ValueError("stockout blueprint exceeds available stores")
        return self


@lru_cache(maxsize=1)
def load_fixed_demo_catalog(path: Path = FIXED_DEMO_CATALOG_PATH) -> FixedDemoCatalog:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("fixed demo catalog must not contain a UTF-8 BOM")
    if len(raw) > 256 * 1024:
        raise ValueError("fixed demo catalog exceeds 256 KiB")
    return FixedDemoCatalog.model_validate_json(raw)


def fixed_demo_catalog_sha256(catalog: FixedDemoCatalog) -> str:
    raw = json.dumps(
        catalog.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def stable_id(case_key: str, entity_type: str, logical_key: str) -> str:
    return str(uuid5(CASE_NAMESPACE, f"{case_key}:{entity_type}:{logical_key}"))


def shanghai_anchor_datetime(anchor_date: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime.combine(anchor_date, time(hour, minute), tzinfo=SHANGHAI).astimezone(UTC)


def today_shanghai() -> date:
    return utcnow().astimezone(SHANGHAI).date()


def validate_fixed_demo_catalog(catalog: FixedDemoCatalog | None = None) -> dict[str, object]:
    catalog = catalog or load_fixed_demo_catalog()
    for item in catalog.attachments:
        source = DEMO_ATTACHMENTS_ROOT / item.file_name
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != item.sha256:
            raise ValueError(f"controlled attachment failed validation: {item.file_name}")
    showcase = load_showcase_catalog()
    scenario_results = []
    for scenario in showcase.scenarios:
        first = calculate_scenario(scenario)
        second = calculate_scenario(scenario)
        if first != second:
            raise ValueError(f"showcase scenario is not deterministic: {scenario.public_key}")
        carpool, warehouse, _ = first
        eligible_warehouses = [
            candidate
            for group in warehouse.get("candidates", [])
            for candidate in group.get("candidate_warehouses", [])
            if candidate.get("eligible")
        ]
        if not carpool.get("candidates") or not eligible_warehouses:
            raise ValueError(f"showcase scenario has no usable allocation: {scenario.public_key}")
        scenario_results.append(
            {
                "key": scenario.public_key,
                "carpool_candidates": len(carpool.get("candidates", [])),
                "warehouse_candidates": len(warehouse.get("candidates", [])),
            }
        )
    return {
        "case_key": catalog.case_key,
        "catalog_version": catalog.catalog_version,
        "catalog_sha256": fixed_demo_catalog_sha256(catalog),
        "counts": {
            "accounts": len(catalog.accounts),
            "enterprises": len(catalog.enterprises),
            "stores": len(catalog.stores),
            "products": len(catalog.products),
            "warehouses": len(catalog.warehouses),
            "suppliers": len(catalog.suppliers),
            "fleet": len(catalog.fleet),
            "attachments": len(catalog.attachments),
        },
        "scenarios": scenario_results,
    }


def guard_showcase_database(db: Session, *, require_process_role: bool) -> None:
    settings = get_settings()
    database_name = db.get_bind().url.database or ""
    if require_process_role and settings.dataset_role != "showcase":
        raise RuntimeError("DATASET_ROLE must be showcase")
    if db.get_bind().dialect.name == "postgresql" and database_name != "black_soil_loop_showcase":
        raise RuntimeError("fixed demo case may only write black_soil_loop_showcase")
    database_file_name = Path(database_name).name.lower()
    if db.get_bind().dialect.name != "postgresql" and "showcase" not in database_file_name:
        raise RuntimeError("fixed demo tests require an explicitly named showcase database")


def acquire_demo_lock(db: Session, case_key: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        lock_key = int.from_bytes(hashlib.sha256(case_key.encode()).digest()[:8], "big", signed=True)
        locked = db.scalar(text("SELECT pg_try_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
        if not locked:
            raise RuntimeError("fixed demo case is already being refreshed")


def _reset_owned_tables(db: Session) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        if table.key not in PRESERVED_TABLES:
            db.execute(delete(table))


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _event_id(case_key: str, revision: int, topic: str, logical_key: str) -> str:
    return stable_id(case_key, "event", f"{revision}:{topic}:{logical_key}")


def _published_operational_event(
    db: Session,
    *,
    case_key: str,
    revision: int,
    topic: str,
    object_type: str,
    object_id: str,
    logical_key: str,
) -> None:
    event = OutboxEvent(
        event_id=_event_id(case_key, revision, topic, logical_key),
        topic=topic,
        object_type=object_type,
        object_id=object_id,
        object_version=1,
        payload={
            "dataset": "showcase",
            "case_revision": revision,
            "defer_dashboard_refresh": True,
        },
    )
    db.add(event)


def _create_master_data(
    db: Session,
    catalog: FixedDemoCatalog,
    password_hash: str,
) -> dict[str, dict[str, object]]:
    case_key = catalog.case_key
    enterprises: dict[str, Enterprise] = {}
    stores: dict[str, Store] = {}
    products: dict[str, Product] = {}
    warehouses: dict[str, Warehouse] = {}
    suppliers: dict[str, Supplier] = {}
    drivers: dict[str, Driver] = {}
    vehicles: dict[str, Vehicle] = {}
    users: dict[str, User] = {}

    for item in catalog.enterprises:
        row = Enterprise(
            id=stable_id(case_key, "enterprise", item.code),
            code=item.code,
            name=item.name,
            enabled=item.enabled,
        )
        db.add(row)
        enterprises[item.code] = row
    for item in catalog.products:
        row = Product(
            id=stable_id(case_key, "product", item.code),
            code=item.code,
            name=item.name,
            category=item.category,
            unit=item.unit,
            temperature_zone=item.temperature_zone,
        )
        db.add(row)
        products[item.code] = row
    for item in catalog.fleet:
        driver = Driver(
            id=stable_id(case_key, "driver", item.driver_code),
            code=item.driver_code,
            name=item.driver_name,
            enabled=item.enabled,
        )
        vehicle = Vehicle(
            id=stable_id(case_key, "vehicle", item.driver_code),
            driver_id=driver.id,
            plate_no=item.vehicle_label,
            temperature_zone=item.temperature_zone,
            max_weight_kg=item.max_weight_kg,
            max_volume_m3=item.max_volume_m3,
            enabled=item.enabled,
        )
        db.add_all([driver, vehicle])
        drivers[item.driver_code] = driver
        vehicles[item.driver_code] = vehicle
    db.flush()
    for item in catalog.stores:
        row = Store(
            id=stable_id(case_key, "store", item.code),
            enterprise_id=enterprises[item.enterprise_code].id,
            code=item.code,
            name=item.name,
            channel=item.channel,
            latitude=item.latitude,
            longitude=item.longitude,
            enabled=True,
        )
        db.add(row)
        stores[item.code] = row
    for item in catalog.warehouses:
        row = Warehouse(
            id=stable_id(case_key, "warehouse", item.code),
            enterprise_id=enterprises[item.enterprise_code].id,
            code=item.code,
            name=item.name,
            temperature_zone=item.temperature_zone,
            latitude=item.latitude,
            longitude=item.longitude,
            capacity_m3=item.capacity_m3,
            used_m3=item.used_m3,
            reserved_m3=item.reserved_m3,
        )
        db.add(row)
        warehouses[item.code] = row
    for item in catalog.suppliers:
        row = Supplier(
            id=stable_id(case_key, "supplier", item.code),
            code=item.code,
            name=item.name,
            delivery_score=item.delivery_score,
            quality_score=item.quality_score,
        )
        db.add(row)
        suppliers[item.code] = row
    db.flush()

    for item in catalog.accounts:
        row = User(
            id=stable_id(case_key, "user", item.username),
            username=item.username,
            display_name=item.display_name,
            password_hash=password_hash,
            role=item.role,
            enterprise_id=enterprises[item.enterprise_code].id if item.enterprise_code else None,
            store_id=stores[item.store_code].id if item.store_code else None,
            driver_id=drivers[item.driver_code].id if item.driver_code else None,
            active=True,
            session_version=1,
        )
        db.add(row)
        users[item.username] = row
    db.flush()
    return {
        "enterprises": enterprises,
        "stores": stores,
        "products": products,
        "warehouses": warehouses,
        "suppliers": suppliers,
        "drivers": drivers,
        "vehicles": vehicles,
        "users": users,
    }


def _create_supplier_prices(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
    anchor_date: date,
) -> int:
    products = master["products"]
    suppliers = master["suppliers"]
    blueprint = catalog.generation.supplier_pricing
    count = 0
    for product_index, product_item in enumerate(catalog.products):
        for supplier_index, supplier_item in enumerate(catalog.suppliers):
            for tier_index, minimum in enumerate(blueprint.tier_minimums):
                base_price = (
                    blueprint.base_unit_price
                    + product_index * blueprint.product_price_step
                    + supplier_index * blueprint.supplier_price_step
                    - tier_index * blueprint.tier_discount_step
                )
                db.add(
                    SupplierPriceTier(
                        id=stable_id(
                            catalog.case_key,
                            "supplier-tier",
                            f"{supplier_item.code}:{product_item.code}:{tier_index}",
                        ),
                        supplier_id=suppliers[supplier_item.code].id,
                        product_id=products[product_item.code].id,
                        min_quantity=minimum,
                        max_quantity=blueprint.first_tier_maximum if tier_index == 0 else None,
                        supply_capacity=blueprint.capacity_base
                        + supplier_index * blueprint.capacity_step,
                        unit_price=_money(base_price),
                        valid_from=anchor_date - timedelta(days=blueprint.valid_days_before),
                        valid_to=anchor_date + timedelta(days=blueprint.valid_days_after),
                    )
                )
                count += 1
    return count


def _create_orders_and_reports(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
    anchor_date: date,
) -> tuple[list[TransportOrder], int]:
    enterprises = master["enterprises"]
    stores = master["stores"]
    products = master["products"]
    blueprint = catalog.generation.rolling_business
    rolling_orders: list[TransportOrder] = []
    enterprise_items = catalog.enterprises[:5]
    first_offset = -(blueprint.days - 1)
    for offset in range(first_offset, 1):
        day = anchor_date + timedelta(days=offset)
        curve = catalog.daily_curve[offset - first_offset]
        for channel_index, store_item in enumerate((catalog.stores[offset % 6], catalog.stores[6 + offset % 6])):
            product_item = catalog.products[(offset * 2 + channel_index) % len(catalog.products)]
            enterprise_item = enterprise_items[(offset + channel_index) % len(enterprise_items)]
            created_at = shanghai_anchor_datetime(day, 6 + channel_index, 20)
            order = TransportOrder(
                id=stable_id(catalog.case_key, "rolling-order", f"{day.isoformat()}:{channel_index}"),
                order_no=f"XC-{day:%m%d}-{channel_index + 1:02d}",
                scenario_code="FIXED_ROLLING",
                enterprise_id=enterprises[enterprise_item.code].id,
                product_id=products[product_item.code].id,
                store_id=stores[store_item.code].id,
                origin_latitude=blueprint.origin_latitude,
                origin_longitude=blueprint.origin_longitude,
                destination_latitude=store_item.latitude,
                destination_longitude=store_item.longitude,
                departure_at=shanghai_anchor_datetime(day, 8 + channel_index, 0),
                quantity=round(blueprint.quantity_bases[channel_index] * curve, 2),
                unit=product_item.unit,
                weight_kg=round(blueprint.weight_bases_kg[channel_index] * curve, 2),
                volume_m3=round(blueprint.volume_bases_m3[channel_index] * curve, 2),
                temperature_zone=product_item.temperature_zone,
                status="CONFIRMED" if offset > -blueprint.recent_confirmed_days else "COMPLETED",
                created_at=created_at,
                updated_at=created_at,
            )
            db.add(order)
            rolling_orders.append(order)
        for store_index, store_item in enumerate(catalog.stores):
            weight = Decimal(str(store_item.weight))
            base = Decimal(
                str(
                    blueprint.third_space_sales_base
                    if store_item.channel == "THIRD_SPACE"
                    else blueprint.traditional_sales_base
                )
            )
            sales = (base * Decimal(str(curve)) * weight).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            report = StoreDailyReport(
                id=stable_id(catalog.case_key, "daily-report", f"{store_item.code}:{day.isoformat()}"),
                store_id=stores[store_item.code].id,
                report_date=day,
                sales_amount=sales,
                order_count=max(
                    blueprint.minimum_daily_orders,
                    int(float(sales) / blueprint.average_order_value) + store_index % 4,
                ),
                authorized_for_dashboard=True,
                summary={"source": "fixed_case", "channel": store_item.channel},
                idempotency_key=f"fixed-{store_item.code}-{day.isoformat()}",
                created_at=shanghai_anchor_datetime(day, 21, 0),
                updated_at=shanghai_anchor_datetime(day, 21, 0),
            )
            db.add(report)
    db.flush()
    return rolling_orders, len(catalog.stores) * blueprint.days


def _create_inventory_and_stockouts(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
    anchor_date: date,
    revision: int,
) -> tuple[int, int, int]:
    stores = master["stores"]
    products = master["products"]
    blueprint = catalog.generation.inventory
    inventory_count = 0
    for store_index, store_item in enumerate(catalog.stores):
        for product_index, product_item in enumerate(catalog.products[: blueprint.product_count]):
            threshold = blueprint.threshold_base + product_index * blueprint.threshold_step
            quantity = (
                threshold - blueprint.low_stock_delta
                if inventory_count < blueprint.low_stock_count
                else threshold
                if inventory_count < blueprint.low_stock_count + blueprint.critical_stock_count
                else blueprint.healthy_quantity_base
                + store_index * blueprint.healthy_store_step
                + product_index * blueprint.healthy_product_step
            )
            db.add(
                InventoryBalance(
                    id=stable_id(catalog.case_key, "inventory", f"{store_item.code}:{product_item.code}"),
                    store_id=stores[store_item.code].id,
                    product_id=products[product_item.code].id,
                    quantity=quantity,
                    low_stock_threshold=threshold,
                    created_at=shanghai_anchor_datetime(anchor_date, 7, 0),
                    updated_at=shanghai_anchor_datetime(anchor_date, 7, 0),
                )
            )
            inventory_count += 1
    statuses = blueprint.stockout_statuses
    for index, status in enumerate(statuses):
        store_item = catalog.stores[index]
        product_item = catalog.products[index % blueprint.product_count]
        row = StockoutRequest(
            id=stable_id(catalog.case_key, "stockout", str(index + 1)),
            store_id=stores[store_item.code].id,
            product_id=products[product_item.code].id,
            requested_quantity=blueprint.stockout_quantity_base
            + index * blueprint.stockout_quantity_step,
            reason="门店补货需求超过当前可用库存",
            status=status,
            idempotency_key=f"fixed-stockout-{index + 1}",
            created_at=shanghai_anchor_datetime(anchor_date - timedelta(days=index % 3), 9, 0),
            updated_at=shanghai_anchor_datetime(anchor_date - timedelta(days=index % 3), 9, 0),
        )
        db.add(row)
        db.flush()
        _published_operational_event(
            db,
            case_key=catalog.case_key,
            revision=revision,
            topic="store.stockout.submitted",
            object_type="stockout_request",
            object_id=row.id,
            logical_key=str(index + 1),
        )
    return inventory_count, len(statuses), 0


def _create_tasks_and_operations(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
    rolling_orders: list[TransportOrder],
    anchor_date: date,
    revision: int,
) -> tuple[dict[str, int], dict[int, str]]:
    stores = master["stores"]
    products = master["products"]
    vehicles = master["vehicles"]
    drivers = master["drivers"]
    users = master["users"]
    fulfillment = catalog.generation.fulfillment
    inventory = catalog.generation.inventory
    rolling = catalog.generation.rolling_business
    task_statuses = fulfillment.task_statuses
    tasks: list[TransportTask] = []
    qr_tokens: dict[int, str] = {}
    for index, status in enumerate(task_statuses):
        store_item = catalog.stores[index]
        # One dedicated showcase driver can demonstrate every active B02 transition.
        fleet_item = catalog.fleet[0] if index < 5 else catalog.fleet[1 + (index - 5) % 4]
        source_orders = rolling_orders[max(0, len(rolling_orders) - 16 + index * 2) :][-2:]
        qr_token = None
        if status not in {"COMPLETED", "CANCELLED"}:
            qr_token = f"BSL:{catalog.case_key}:{secrets.token_urlsafe(18)}"
            qr_tokens[index] = qr_token
        task = TransportTask(
            id=stable_id(catalog.case_key, "task", str(index + 1)),
            task_no=f"CC-YS-{index + 1:02d}",
            source_order_ids=[item.id for item in source_orders],
            vehicle_id=vehicles[fleet_item.driver_code].id,
            driver_id=drivers[fleet_item.driver_code].id,
            store_id=stores[store_item.code].id,
            status=status,
            temperature_zone=fleet_item.temperature_zone,
            total_weight_kg=fulfillment.task_weight_base_kg
            + index * fulfillment.task_weight_step_kg,
            total_volume_m3=fulfillment.task_volume_base_m3
            + index * fulfillment.task_volume_step_m3,
            origin_latitude=rolling.origin_latitude,
            origin_longitude=rolling.origin_longitude,
            destination_latitude=store_item.latitude,
            destination_longitude=store_item.longitude,
            planned_departure_at=shanghai_anchor_datetime(anchor_date, 7 + index % 3, index * 4 % 60),
            qr_token_hash=hashlib.sha256(qr_token.encode("utf-8")).hexdigest() if qr_token else None,
            qr_expires_at=shanghai_anchor_datetime(anchor_date + timedelta(days=1), 3, 5)
            if qr_token
            else None,
            created_at=shanghai_anchor_datetime(anchor_date - timedelta(days=1), 18, index),
            updated_at=shanghai_anchor_datetime(anchor_date, 8, index),
        )
        db.add(task)
        tasks.append(task)
        db.flush()
        stop_count = 3 if index in {5, 6} else 1 + index % 3
        for stop_index in range(stop_count):
            target = catalog.stores[(index + stop_index) % len(catalog.stores)]
            stop_status = "DELIVERED" if status in {"DELIVERED", "COMPLETED"} else "PLANNED"
            db.add(
                TaskStop(
                    id=stable_id(catalog.case_key, "task-stop", f"{index + 1}:{stop_index + 1}"),
                    task_id=task.id,
                    store_id=stores[target.code].id,
                    sequence_no=stop_index + 1,
                    latitude=target.latitude,
                    longitude=target.longitude,
                    status=stop_status,
                    delivery_lines=[
                        {
                            "product_id": products[
                                catalog.products[(index + stop_index) % inventory.product_count].code
                            ].id,
                            "expected_quantity": 30 + index * 2,
                            "unit": catalog.products[
                                (index + stop_index) % inventory.product_count
                            ].unit,
                        }
                    ],
                )
            )
        if status != "PUBLISHED":
            db.add(
                TaskEvent(
                    id=stable_id(catalog.case_key, "task-event", str(index + 1)),
                    task_id=task.id,
                    from_status="PUBLISHED",
                    to_status=status,
                    action="FIXED_CASE_RESTORE",
                    actor_user_id=users["showcase-admin"].id,
                    idempotency_key=f"fixed-task-event-{revision}-{index + 1}",
                    payload={"fixed_case": True},
                    occurred_at=shanghai_anchor_datetime(anchor_date, 8, index),
                )
            )
    db.flush()

    for index, task in enumerate(tasks[: len(fulfillment.location_age_minutes)]):
        age_minutes = fulfillment.location_age_minutes[index]
        store_item = catalog.stores[index]
        db.add(
            LocationPoint(
                id=stable_id(catalog.case_key, "location", str(index + 1)),
                task_id=task.id,
                driver_id=task.driver_id,
                latitude=(rolling.origin_latitude + store_item.latitude) / 2,
                longitude=(rolling.origin_longitude + store_item.longitude) / 2,
                speed_mps=8.4 + index,
                accuracy_m=6 + index * 2,
                recorded_at=utcnow() - timedelta(minutes=age_minutes),
                idempotency_key=f"fixed-location-{revision}-{index + 1}",
            )
        )
        anomaly = None if index < 2 else "HIGH_TEMPERATURE" if index == 2 else "HIGH_HUMIDITY" if index == 3 else None
        db.add(
            TelemetryPoint(
                id=stable_id(catalog.case_key, "telemetry", str(index + 1)),
                task_id=task.id,
                vehicle_id=task.vehicle_id,
                sampled_at=utcnow() - timedelta(minutes=min(age_minutes, 20)),
                temperature_c=4.2 if index < 2 else 11.8 if index == 2 else 5.1,
                humidity_pct=62 if index < 3 else 89,
                latitude=(rolling.origin_latitude + store_item.latitude) / 2,
                longitude=(rolling.origin_longitude + store_item.longitude) / 2,
                anomaly_code=anomaly,
            )
        )
    alert_states = fulfillment.alert_states
    alert_types = fulfillment.alert_types
    for index, status in enumerate(alert_states):
        opened = utcnow() - timedelta(minutes=22 + index * 15)
        db.add(
            Alert(
                id=stable_id(catalog.case_key, "alert", str(index + 1)),
                task_id=tasks[index + 2].id,
                alert_type=alert_types[index],
                status=status,
                message="运输温控指标超过预设范围" if index % 2 == 0 else "运输湿度指标超过预设范围",
                opened_at=opened,
                acknowledged_at=opened + timedelta(minutes=5) if status != "OPEN" else None,
                resolved_at=opened + timedelta(minutes=18) if status == "RESOLVED" else None,
                actor_user_id=users["showcase-admin"].id if status != "OPEN" else None,
            )
        )
    issue_types = fulfillment.telemetry_issue_types
    for index, issue_type in enumerate(issue_types):
        issue = TelemetryIssue(
            id=stable_id(catalog.case_key, "telemetry-issue", str(index + 1)),
            source_key=f"fixed:{revision}:issue:{index + 1}",
            task_id=tasks[index + 2].id,
            store_id=stores[catalog.stores[index + 2].code].id,
            issue_type=issue_type,
            source_type="FIXED_CASE",
            severity="HIGH" if index < 2 else "MEDIUM",
            status="RESOLVED" if index == 3 else "OPEN",
            task_status=tasks[index + 2].status,
            expected_vehicle_id=tasks[index + 2].vehicle_id,
            actual_vehicle_id=(
                vehicles[catalog.fleet[1].driver_code].id
                if issue_type == "VEHICLE_MISMATCH"
                else tasks[index + 2].vehicle_id
            ),
            expected_driver_id=tasks[index + 2].driver_id,
            actual_driver_id=(
                drivers[catalog.fleet[2].driver_code].id
                if issue_type == "DRIVER_MISMATCH"
                else tasks[index + 2].driver_id
            ),
            message="固定案例遥测一致性检查记录",
            details={"fixed_case": True},
            occurred_at=utcnow() - timedelta(minutes=35 + index * 10),
            resolved_at=utcnow() - timedelta(minutes=5) if index == 3 else None,
        )
        db.add(issue)
        db.flush()
        _published_operational_event(
            db,
            case_key=catalog.case_key,
            revision=revision,
            topic="telemetry.issue.recorded",
            object_type="telemetry_issue",
            object_id=issue.id,
            logical_key=str(index + 1),
        )

    receipt_statuses = fulfillment.receipt_statuses
    movement_count = 0
    for index, receipt_status in enumerate(receipt_statuses):
        task_index = 5 if index < 3 else 6
        stop_offset = index if index < 3 else index - 3
        product_item = catalog.products[index % inventory.product_count]
        store_item = catalog.stores[(task_index + stop_offset) % len(catalog.stores)]
        expected = 30 + index * 4
        received = expected if receipt_status == "FULL" else expected - 5 if receipt_status == "PARTIAL" else 0
        receipt = Receipt(
            id=stable_id(catalog.case_key, "receipt", str(index + 1)),
            task_id=tasks[task_index].id,
            store_id=stores[store_item.code].id,
            receipt_status=receipt_status,
            details=[
                {
                    "product_id": products[product_item.code].id,
                    "expected_quantity": expected,
                    "received_quantity": received,
                    "unit": product_item.unit,
                    "reason": "包装破损" if receipt_status == "REJECTED" else None,
                }
            ],
            idempotency_key=f"fixed-receipt-{revision}-{index + 1}",
            actor_user_id=users["showcase-admin"].id,
            received_at=shanghai_anchor_datetime(anchor_date, 15, index * 3),
        )
        db.add(receipt)
        db.flush()
        if received > 0:
            movement = InventoryMovement(
                id=stable_id(catalog.case_key, "receipt-movement", str(index + 1)),
                store_id=stores[store_item.code].id,
                product_id=products[product_item.code].id,
                movement_type="IN",
                quantity_before=40,
                quantity_delta=received,
                quantity_after=40 + received,
                quantity_context="RECORDED",
                source_type="RECEIPT",
                source_id=receipt.id,
                idempotency_key=f"fixed-receipt-movement-{revision}-{index + 1}",
                occurred_at=receipt.received_at,
            )
            db.add(movement)
            movement_count += 1
        _published_operational_event(
            db,
            case_key=catalog.case_key,
            revision=revision,
            topic="store.receipt.completed",
            object_type="receipt",
            object_id=receipt.id,
            logical_key=str(index + 1),
        )
    for index, delta in enumerate(inventory.manual_movement_deltas):
        store_item = catalog.stores[6 + index % 6]
        product_item = catalog.products[index % inventory.product_count]
        movement = InventoryMovement(
                id=stable_id(catalog.case_key, "manual-movement", str(index + 1)),
                store_id=stores[store_item.code].id,
                product_id=products[product_item.code].id,
                movement_type="OUT" if delta < 0 else "ADJUST",
                quantity_before=80,
                quantity_delta=delta,
                quantity_after=80 + delta,
                quantity_context="RECORDED",
                source_type="MANUAL_ADJUSTMENT",
                source_id=stable_id(catalog.case_key, "adjustment", str(index + 1)),
                idempotency_key=f"fixed-manual-movement-{revision}-{index + 1}",
                occurred_at=shanghai_anchor_datetime(anchor_date, 16, index * 4),
            )
        db.add(movement)
        db.flush()
        db.add(
            InventoryMovementProjection(
                id=stable_id(catalog.case_key, "manual-movement-projection", str(index + 1)),
                source_movement_id=movement.id,
                source_event_id=_event_id(catalog.case_key, revision, "inventory.movement.recorded", str(index + 1)),
                source_version=1,
                store_id=movement.store_id,
                product_id=movement.product_id,
                movement_type=movement.movement_type,
                quantity_before=movement.quantity_before,
                quantity_delta=movement.quantity_delta,
                quantity_after=movement.quantity_after,
                quantity_context=movement.quantity_context,
                source_type=movement.source_type,
                source_id=movement.source_id,
                business_at=movement.occurred_at,
            )
        )
        movement_count += 1
    db.add(
        TaskException(
            id=stable_id(catalog.case_key, "task-exception", "cancelled"),
            task_id=tasks[-1].id,
            exception_type="DELIVERY_CANCELLED",
            reason="门店临时闭店，任务已按流程取消",
            original_status="PUBLISHED",
            actor_user_id=users["showcase-admin"].id,
            idempotency_key=f"fixed-exception-{revision}",
            occurred_at=shanghai_anchor_datetime(anchor_date, 9, 40),
        )
    )
    return {
        "tasks": len(tasks),
        "task_stops": sum(3 if index in {5, 6} else 1 + index % 3 for index in range(len(tasks))),
        "locations": len(fulfillment.location_age_minutes),
        "telemetry": len(fulfillment.location_age_minutes),
        "alerts": len(alert_states),
        "telemetry_issues": len(issue_types),
        "receipts": len(receipt_statuses),
        "inventory_movements": movement_count,
    }, qr_tokens


def _project_daily_reports(
    db: Session,
    catalog: FixedDemoCatalog,
    revision: int,
) -> int:
    reports = list(db.scalars(select(StoreDailyReport).order_by(StoreDailyReport.report_date, StoreDailyReport.id)))
    for report in reports:
        _published_operational_event(
            db,
            case_key=catalog.case_key,
            revision=revision,
            topic="store.daily_report.submitted",
            object_type="store_daily_report",
            object_id=report.id,
            logical_key=f"{report.store_id}:{report.report_date.isoformat()}",
        )
    return len(reports)


def _create_forecast_and_procurement_inputs(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
    anchor_date: date,
) -> tuple[int, int]:
    planning = catalog.generation.planning
    enabled_enterprises = [item for item in catalog.enterprises if item.enabled][
        : planning.forecast_enterprise_count
    ]
    products = master["products"]
    enterprises = master["enterprises"]
    forecast_start, forecast_end = next_week_window(shanghai_anchor_datetime(anchor_date, 12, 0))
    history_count = 0
    plan_count = 0
    for enterprise_index, enterprise_item in enumerate(enabled_enterprises):
        for product_index, product_item in enumerate(catalog.products[: planning.product_count]):
            points = planning.history_points_by_product[product_index]
            for point_index in range(points):
                period_start = anchor_date - timedelta(days=(points - point_index) * 7)
                quantity = 160 + enterprise_index * 13 + product_index * 21 + point_index * 3.5
                if product_index < 2:
                    quantity += (point_index % 12) * 4.2
                db.add(
                    DemandHistory(
                        id=stable_id(
                            catalog.case_key,
                            "demand-history",
                            f"{enterprise_item.code}:{product_item.code}:{period_start.isoformat()}",
                        ),
                        enterprise_id=enterprises[enterprise_item.code].id,
                        product_id=products[product_item.code].id,
                        period_start=period_start,
                        quantity=round(quantity, 2),
                    )
                )
                history_count += 1
            if product_index != 5:
                for day_index in range(7):
                    db.add(
                        ProductionPlan(
                            id=stable_id(
                                catalog.case_key,
                                "production-plan",
                                (
                                    f"{enterprise_item.code}:{product_item.code}:"
                                    f"{forecast_start + timedelta(days=day_index)}"
                                ),
                            ),
                            enterprise_id=enterprises[enterprise_item.code].id,
                            product_id=products[product_item.code].id,
                            plan_date=forecast_start + timedelta(days=day_index),
                            planned_quantity=38 + enterprise_index * 3 + product_index * 4 + day_index,
                        )
                    )
                    plan_count += 1
    return history_count, plan_count


def _generate_procurement_and_forecasts(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
    anchor_date: date,
    revision: int,
) -> dict[str, int]:
    planning = catalog.generation.planning
    enabled_enterprises = [item for item in catalog.enterprises if item.enabled]
    enterprises = master["enterprises"]
    products = master["products"]
    users = master["users"]
    cycle_start, cycle_end = procurement_cycle(now=shanghai_anchor_datetime(anchor_date, 12, 0))
    for index, product_item in enumerate(catalog.products[: planning.product_count]):
        enterprise_item = enabled_enterprises[index % len(enabled_enterprises)]
        db.add(
            ProcurementDemandConfirmation(
                id=stable_id(catalog.case_key, "procurement-confirmation", product_item.code),
                enterprise_id=enterprises[enterprise_item.code].id,
                product_id=products[product_item.code].id,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
                quantity=planning.procurement_quantity_base
                + index * planning.procurement_quantity_step,
                unit="托" if index == planning.unit_warning_product_index else product_item.unit,
                status="CONFIRMED",
                reason="园区当前采购周期固定需求确认",
                confirmed_by=users["showcase-admin"].id,
                confirmed_at=shanghai_anchor_datetime(anchor_date, 11, index),
                created_at=shanghai_anchor_datetime(anchor_date, 11, index),
                updated_at=shanghai_anchor_datetime(anchor_date, 11, index),
            )
        )
    db.flush()
    target_product_ids = {
        products[item.code].id for item in catalog.products[: planning.product_count]
    }
    _, _, aggregations = generate_procurement_aggregations(
        db,
        cycle_start,
        product_ids=target_product_ids,
        id_factory=lambda product, cycle: stable_id(
            catalog.case_key,
            "procurement-aggregation",
            f"{product.id}:{cycle.isoformat()}",
        ),
    )
    for index, aggregation in enumerate(aggregations):
        if index < planning.confirmed_aggregation_count:
            aggregation.status = "CONFIRMED"
            aggregation.confirmed_by = users["showcase-admin"].id
            aggregation.confirmed_at = shanghai_anchor_datetime(anchor_date, 13, index)
        elif index == planning.adjusted_aggregation_index:
            aggregation.adjusted_quantity = round(
                aggregation.automatic_quantity * planning.adjustment_factor, 2
            )
            aggregation.adjustment_reason = "结合园区周转库存进行受控调整"
        db.add(
            OutboxEvent(
                event_id=_event_id(
                    catalog.case_key,
                    revision,
                    "procurement.aggregation.generated",
                    str(index + 1),
                ),
                topic="procurement.aggregation.generated",
                object_type="procurement_aggregation",
                object_id=aggregation.id,
                object_version=aggregation.object_version,
                payload={"defer_dashboard_refresh": True},
            )
        )
    batch, projections, _ = generate_next_week_forecasts(
        db,
        scheduled=True,
        now=shanghai_anchor_datetime(anchor_date, 12, 0),
        enterprise_ids={
            enterprises[item.code].id
            for item in enabled_enterprises[: planning.forecast_enterprise_count]
        },
        product_ids=target_product_ids,
        projection_id_factory=lambda enterprise, product, forecast_start: stable_id(
            catalog.case_key,
            "forecast-projection",
            f"{enterprise.id}:{product.id}:{forecast_start.isoformat()}",
        ),
        batch_id_factory=lambda forecast_start: stable_id(
            catalog.case_key,
            "forecast-batch",
            forecast_start.isoformat(),
        ),
    )
    db.add(
        OutboxEvent(
            event_id=_event_id(catalog.case_key, revision, "demand.forecast.generated", "next-week"),
            topic="demand.forecast.generated",
            object_type="demand_forecast_batch",
            object_id=batch.id,
            object_version=batch.object_version,
            payload={"defer_dashboard_refresh": True},
        )
    )
    return {
        "procurement_confirmations": planning.product_count,
        "procurement_aggregations": len(aggregations),
        "forecast_projections": len(projections),
        "forecast_batches": 1 if batch else 0,
    }


def _create_algorithm_showcase_and_plans(
    db: Session,
    catalog: FixedDemoCatalog,
    anchor_date: date,
    revision: int,
    master: dict[str, dict[str, object]],
    qr_tokens: dict[int, str],
) -> dict[str, int]:
    showcase = load_showcase_catalog()
    batch_key = f"fixed-demo-{catalog.catalog_version}"
    runs: list[AlgorithmRun] = []
    for scenario in showcase.scenarios:
        signature = catalog_sha256(scenario)
        carpool, warehouse, mappings = calculate_scenario(scenario)
        for algorithm_type, result in (("CARPOOL", carpool), ("WAREHOUSE", warehouse)):
            run = AlgorithmRun(
                id=stable_id(catalog.case_key, "algorithm-run", f"{algorithm_type}:{scenario.public_key}"),
                algorithm_type=algorithm_type,
                scenario_code=scenario.scenario_code,
                rules_version=result["rules_version"],
                input_snapshot={
                    "catalog_version": showcase.catalog_version,
                    "public_key": scenario.public_key,
                    **mappings,
                },
                output_snapshot=result,
                showcase_key=scenario.public_key,
                showcase_batch_key=batch_key,
                input_signature=signature,
                input_data_cutoff=None,
                showcase_enabled=True,
                showcase_source_mode="PRESET_SIMULATION",
                created_at=shanghai_anchor_datetime(anchor_date, 8, 30),
            )
            db.add(run)
            runs.append(run)
            db.add(
                OutboxEvent(
                    event_id=_event_id(
                        catalog.case_key,
                        revision,
                        "algorithm.run.completed",
                        f"{algorithm_type}:{scenario.public_key}",
                    ),
                    topic="algorithm.run.completed",
                    object_type="algorithm_run",
                    object_id=run.id,
                    object_version=1,
                    payload={
                        "algorithm_type": algorithm_type,
                        "showcase_key": scenario.public_key,
                        "defer_dashboard_refresh": True,
                    },
                )
            )
    db.flush()
    carpool_runs = [item for item in runs if item.algorithm_type == "CARPOOL"]
    warehouse_runs = [item for item in runs if item.algorithm_type == "WAREHOUSE"]
    planning = catalog.generation.planning
    plan_statuses = planning.transport_plan_statuses
    for index in range(len(plan_statuses)):
        showcase_run = carpool_runs[index % len(carpool_runs)]
        plan_run = AlgorithmRun(
            id=stable_id(catalog.case_key, "transport-plan-run", str(index + 1)),
            algorithm_type="CARPOOL",
            scenario_code=showcase_run.scenario_code,
            rules_version=showcase_run.rules_version,
            input_snapshot=showcase_run.input_snapshot,
            output_snapshot=showcase_run.output_snapshot,
            showcase_enabled=False,
            created_at=shanghai_anchor_datetime(anchor_date, 8, 35 + index),
        )
        db.add(plan_run)
        candidates = showcase_run.output_snapshot.get("candidates", [])
        candidate = candidates[index % len(candidates)] if candidates else {}
        db.add(
            TransportPlan(
                id=stable_id(catalog.case_key, "transport-plan", str(index + 1)),
                plan_no=f"CC-PL-{index + 1:02d}",
                algorithm_run_id=plan_run.id,
                candidate_snapshot=candidate,
                status=plan_statuses[index],
                task_id=stable_id(catalog.case_key, "task", str(index + 1)),
                published_qr_token=qr_tokens.get(index),
                qr_expires_at=(
                    shanghai_anchor_datetime(anchor_date + timedelta(days=1), 3, 5)
                    if index in qr_tokens
                    else None
                ),
                cancellation_reason="门店时间窗变化" if plan_statuses[index] == "CANCELLED" else None,
            )
        )
    reservation_statuses = planning.reservation_statuses
    pool_statuses = planning.warehouse_plan_statuses
    warehouse_values = list(master["warehouses"].values())
    for index in range(len(pool_statuses)):
        showcase_run = warehouse_runs[index % len(warehouse_runs)]
        pool_run = AlgorithmRun(
            id=stable_id(catalog.case_key, "warehouse-pool-run", str(index + 1)),
            algorithm_type="WAREHOUSE",
            scenario_code=showcase_run.scenario_code,
            rules_version=showcase_run.rules_version,
            input_snapshot=showcase_run.input_snapshot,
            output_snapshot=showcase_run.output_snapshot,
            showcase_enabled=False,
            created_at=shanghai_anchor_datetime(anchor_date, 8, 50 + index),
        )
        db.add(pool_run)
        warehouse = warehouse_values[index % len(warehouse_values)]
        pool = WarehousePoolPlan(
            id=stable_id(catalog.case_key, "warehouse-pool-plan", str(index + 1)),
            plan_no=f"CC-WP-{index + 1:02d}",
            algorithm_run_id=pool_run.id,
            warehouse_id=warehouse.id,
            order_ids=[stable_id(catalog.case_key, "rolling-order", f"{anchor_date.isoformat()}:{index % 2}")],
            required_volume_m3=16 + index * 3,
            inbound_start=shanghai_anchor_datetime(anchor_date, 7, index * 5),
            inbound_end=shanghai_anchor_datetime(anchor_date, 8, index * 5),
            outbound_start=shanghai_anchor_datetime(anchor_date, 15, index * 5),
            outbound_end=shanghai_anchor_datetime(anchor_date, 16, index * 5),
            status=pool_statuses[index],
            reservation_expires_at=shanghai_anchor_datetime(anchor_date + timedelta(days=1), 8, 0),
        )
        db.add(pool)
        db.flush()
        db.add(
            WarehouseReservation(
                id=stable_id(catalog.case_key, "warehouse-reservation", str(index + 1)),
                warehouse_pool_plan_id=pool.id,
                warehouse_id=warehouse.id,
                reserved_volume_m3=pool.required_volume_m3,
                status=reservation_statuses[index],
                expires_at=shanghai_anchor_datetime(anchor_date + timedelta(days=1), 8, 0),
                occupied_at=shanghai_anchor_datetime(anchor_date, 9, 0) if index in {1, 2} else None,
                released_at=shanghai_anchor_datetime(anchor_date, 17, 0) if index == 2 else None,
            )
        )
    return {
        "algorithm_runs": len(runs) + len(plan_statuses) + len(pool_statuses),
        "transport_plans": len(plan_statuses),
        "warehouse_pool_plans": len(pool_statuses),
        "warehouse_reservations": len(reservation_statuses),
    }


def _create_map_points(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
) -> int:
    map_catalog = load_map_catalog()
    map_hash = catalog_sha256(map_catalog)
    stores_by_code = master["stores"]
    for item in map_catalog.points:
        store = stores_by_code.get(item.store_code or "")
        db.add(
            DashboardMapPoint(
                id=stable_id(catalog.case_key, "map-point", item.point_code),
                point_code=item.point_code,
                store_id=store.id if store else None,
                display_name=item.display_name,
                point_type=item.point_type,
                brand_name=item.brand_name,
                address=item.address,
                longitude=item.longitude,
                latitude=item.latitude,
                coordinate_crs=item.coordinate_crs,
                coordinate_accuracy=item.coordinate_accuracy,
                source_longitude=item.source_longitude,
                source_latitude=item.source_latitude,
                source_crs=item.source_crs,
                conversion_method=item.conversion_method,
                featured=item.featured,
                display_order=item.display_order,
                source_type=item.source_type,
                source_name=item.source_name,
                source_url=str(item.source_url) if item.source_url else None,
                source_accessed_at=item.source_accessed_at,
                verified_at=item.verified_at,
                catalog_version=map_catalog.catalog_version,
                catalog_sha256=map_hash,
                enabled=item.enabled,
            )
        )
    return len(map_catalog.points)


def _install_attachments(
    db: Session,
    catalog: FixedDemoCatalog,
    master: dict[str, dict[str, object]],
) -> int:
    settings = get_settings()
    upload_root = Path(getattr(settings, "showcase_upload_dir", settings.upload_dir)).resolve()
    upload_root.mkdir(parents=True, exist_ok=True)
    owner = master["users"]["showcase-admin"]
    for item in catalog.attachments:
        source = (DEMO_ATTACHMENTS_ROOT / item.file_name).resolve()
        storage_name = f"showcase-{item.file_name}"
        target = (upload_root / storage_name).resolve()
        if target.parent != upload_root:
            raise RuntimeError("controlled attachment target is outside upload directory")
        if hashlib.sha256(source.read_bytes()).hexdigest() != item.sha256:
            raise RuntimeError(f"controlled attachment hash mismatch: {item.file_name}")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=upload_root,
                prefix=f".{storage_name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(source.read_bytes())
                temporary_path = Path(temporary.name)
            temporary_path.chmod(0o644)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        db.add(
            Attachment(
                id=stable_id(catalog.case_key, "attachment", item.purpose),
                owner_user_id=owner.id,
                purpose=item.purpose,
                original_name=item.file_name,
                storage_name=storage_name,
                content_type="text/plain; charset=utf-8",
                size_bytes=source.stat().st_size,
                sha256=item.sha256,
            )
        )
    return len(catalog.attachments)


def _installation_status(installation: DemoCaseInstallation) -> dict[str, object]:
    return {
        "case_key": installation.case_key,
        "catalog_version": installation.catalog_version,
        "catalog_sha256": installation.catalog_sha256,
        "anchor_date": installation.anchor_date.isoformat(),
        "state": installation.state,
        "case_revision": installation.case_revision,
        "refreshed_at": installation.refreshed_at.isoformat(),
    }


def read_fixed_demo_status(db: Session, case_key: str) -> dict[str, object] | None:
    installation = db.scalar(select(DemoCaseInstallation).where(DemoCaseInstallation.case_key == case_key))
    return _installation_status(installation) if installation else None


def materialize_fixed_demo_case(
    db: Session,
    *,
    catalog: FixedDemoCatalog | None = None,
    anchor_date: date | None = None,
    password: SecretStr | None = None,
    reset_reason: str = "INITIAL_APPLY",
    expected_case_revision: int | None = None,
    force_reset: bool = False,
    require_process_role: bool = True,
    active_reset_event_id: str | None = None,
) -> dict[str, object]:
    catalog = catalog or load_fixed_demo_catalog()
    anchor_date = anchor_date or today_shanghai()
    guard_showcase_database(db, require_process_role=require_process_role)
    acquire_demo_lock(db, catalog.case_key)
    digest = fixed_demo_catalog_sha256(catalog)
    installation = db.scalar(
        select(DemoCaseInstallation)
        .where(DemoCaseInstallation.case_key == catalog.case_key)
        .with_for_update()
    )
    if expected_case_revision is not None:
        current_revision = installation.case_revision if installation else 0
        if current_revision != expected_case_revision:
            raise RuntimeError("fixed demo case revision changed; reload status and retry")
    if (
        installation
        and not force_reset
        and installation.catalog_version == catalog.catalog_version
        and installation.catalog_sha256 == digest
        and installation.anchor_date == anchor_date
        and installation.state in {"READY", "REFRESHING"}
    ):
        result = "unchanged" if installation.state == "READY" else "refreshing"
        return {"result": result, **_installation_status(installation), "counts": {}}

    existing_password_hash = db.scalar(
        select(User.password_hash).where(User.username == catalog.accounts[0].username)
    )
    if password is not None:
        if len(password.get_secret_value()) < 12:
            raise RuntimeError("fixed showcase password must contain at least 12 characters")
        password_hash = hash_password(password.get_secret_value())
    elif existing_password_hash:
        password_hash = existing_password_hash
    else:
        raise RuntimeError("initial fixed demo installation requires a root-only password file")

    if force_reset:
        unsettled_events = update(OutboxEvent).where(
            OutboxEvent.status.in_(("PENDING", "PROCESSING"))
        )
        if active_reset_event_id is not None:
            unsettled_events = unsettled_events.where(
                OutboxEvent.event_id != active_reset_event_id
            )
        db.execute(
            unsettled_events
            .values(status="SUPERSEDED", claimed_by=None, claimed_at=None)
            .execution_options(synchronize_session=False)
        )
        db.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.status == "DEAD",
                OutboxEvent.topic == "demo.case.reset_requested",
            )
            .values(status="SUPERSEDED", claimed_by=None, claimed_at=None)
            .execution_options(synchronize_session=False)
        )

    revision = (installation.case_revision + 1) if installation else 1
    now = utcnow()
    if installation is None:
        installation = DemoCaseInstallation(
            id=stable_id(catalog.case_key, "installation", catalog.case_key),
            case_key=catalog.case_key,
            catalog_version=catalog.catalog_version,
            catalog_sha256=digest,
            anchor_date=anchor_date,
            state="REFRESHING",
            case_revision=revision,
            installed_at=now,
            refreshed_at=now,
            last_reset_reason=reset_reason[:120],
        )
        db.add(installation)
        db.flush()
    else:
        installation.state = "REFRESHING"
        installation.case_revision = revision
        installation.anchor_date = anchor_date
        installation.catalog_version = catalog.catalog_version
        installation.catalog_sha256 = digest
        installation.last_reset_reason = reset_reason[:120]
        installation.refreshed_at = now
        db.flush()

    _reset_owned_tables(db)
    master = _create_master_data(db, catalog, password_hash)
    counts: dict[str, int] = {
        "accounts": len(catalog.accounts),
        "enterprises": len(catalog.enterprises),
        "stores": len(catalog.stores),
        "products": len(catalog.products),
        "warehouses": len(catalog.warehouses),
        "suppliers": len(catalog.suppliers),
        "fleet": len(catalog.fleet),
    }
    counts["supplier_price_tiers"] = _create_supplier_prices(db, catalog, master, anchor_date)
    rolling_orders, report_count = _create_orders_and_reports(db, catalog, master, anchor_date)
    counts["rolling_orders"] = len(rolling_orders)
    counts["daily_reports"] = report_count
    history_count, plan_count = _create_forecast_and_procurement_inputs(db, catalog, master, anchor_date)
    counts["demand_history"] = history_count
    counts["production_plans"] = plan_count
    inventory_count, stockout_count, _ = _create_inventory_and_stockouts(
        db, catalog, master, anchor_date, revision
    )
    counts["inventory_balances"] = inventory_count
    counts["stockout_requests"] = stockout_count
    task_counts, qr_tokens = _create_tasks_and_operations(
        db, catalog, master, rolling_orders, anchor_date, revision
    )
    counts.update(task_counts)
    counts["daily_report_projections"] = _project_daily_reports(db, catalog, revision)
    counts.update(_generate_procurement_and_forecasts(db, catalog, master, anchor_date, revision))
    counts.update(
        _create_algorithm_showcase_and_plans(
            db, catalog, anchor_date, revision, master, qr_tokens
        )
    )
    counts["map_points"] = _create_map_points(db, catalog, master)
    counts["attachments"] = _install_attachments(db, catalog, master)
    db.flush()
    db.add(
        OutboxEvent(
            event_id=_event_id(
                catalog.case_key,
                revision,
                "dashboard.projection.refresh_requested",
                "all",
            ),
            topic="dashboard.projection.refresh_requested",
            object_type="dashboard_projection",
            object_id="public-dashboard",
            object_version=revision,
            payload={
                "kinds": ["operations", "map", "algorithm_showcase"],
                "periods": ["7d", "30d", "month"],
                "dataset_mode": "showcase",
                "mark_showcase_ready": True,
                "showcase_case_key": catalog.case_key,
                "showcase_case_revision": revision,
            },
        )
    )
    db.add(
        AuditLog(
            id=stable_id(catalog.case_key, "audit", f"{revision}:{reset_reason}"),
            trace_id=stable_id(catalog.case_key, "trace", str(revision)),
            actor_user_id=None,
            action="RESET_FIXED_DEMO_CASE" if force_reset else "INSTALL_FIXED_DEMO_CASE",
            object_type="demo_case_installation",
            object_id=installation.id,
            before_snapshot=None,
            after_snapshot={
                "catalog_version": catalog.catalog_version,
                "catalog_sha256": digest,
                "anchor_date": anchor_date.isoformat(),
                "case_revision": revision,
                "counts": counts,
            },
        )
    )
    installation.state = "REFRESHING"
    installation.last_reset_reason = reset_reason[:120]
    db.flush()
    return {"result": "applied" if revision == 1 else "reset", **_installation_status(installation), "counts": counts}


def parse_anchor_date(value: str) -> date:
    if value == "today":
        return today_shanghai()
    return date.fromisoformat(value)


def clear_showcase_sessions(db: Session) -> None:
    db.execute(delete(UserSession))
    db.execute(
        text("UPDATE iam.users SET session_version = session_version + 1")
        if db.get_bind().dialect.name == "postgresql"
        else text("UPDATE users SET session_version = session_version + 1")
    )
