from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.shared.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class TimestampVersionMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    object_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, object]:
        # SQLAlchemy includes the previously loaded version in every ORM UPDATE
        # and raises StaleDataError when another transaction wins the race.
        return {"version_id_col": cls.object_version}


class User(Base, TimestampVersionMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_iam_users_username"), {"schema": "iam"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    enterprise_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    store_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    driver_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    wechat_openid: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = ({"schema": "iam"},)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("iam.users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Enterprise(Base, TimestampVersionMixin):
    __tablename__ = "enterprises"
    __table_args__ = (UniqueConstraint("code", name="uq_core_enterprises_code"), {"schema": "core"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Store(Base, TimestampVersionMixin):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("code", name="uq_core_stores_code"), {"schema": "core"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("core.enterprises.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Product(Base, TimestampVersionMixin):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("code", name="uq_core_products_code"), {"schema": "core"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    temperature_zone: Mapped[str] = mapped_column(String(30), nullable=False)


class Driver(Base, TimestampVersionMixin):
    __tablename__ = "drivers"
    __table_args__ = (UniqueConstraint("code", name="uq_core_drivers_code"), {"schema": "core"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Vehicle(Base, TimestampVersionMixin):
    __tablename__ = "vehicles"
    __table_args__ = (UniqueConstraint("plate_no", name="uq_core_vehicles_plate"), {"schema": "core"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    driver_id: Mapped[str | None] = mapped_column(ForeignKey("core.drivers.id"), nullable=True, index=True)
    plate_no: Mapped[str] = mapped_column(String(30), nullable=False)
    temperature_zone: Mapped[str] = mapped_column(String(30), nullable=False)
    max_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    max_volume_m3: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Warehouse(Base, TimestampVersionMixin):
    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("code", name="uq_core_warehouses_code"), {"schema": "core"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("core.enterprises.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    temperature_zone: Mapped[str] = mapped_column(String(30), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    capacity_m3: Mapped[float] = mapped_column(Float, nullable=False)
    used_m3: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    reserved_m3: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class Supplier(Base, TimestampVersionMixin):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("code", name="uq_core_suppliers_code"), {"schema": "core"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    delivery_score: Mapped[float] = mapped_column(Float, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)


class SupplierPriceTier(Base):
    __tablename__ = "supplier_price_tiers"
    __table_args__ = ({"schema": "core"},)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("core.suppliers.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    min_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    max_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    supply_capacity: Mapped[float] = mapped_column(Float, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False)


class TransportOrder(Base, TimestampVersionMixin):
    __tablename__ = "transport_orders"
    __table_args__ = (UniqueConstraint("order_no", name="uq_b01_transport_orders_no"), {"schema": "b01"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_no: Mapped[str] = mapped_column(String(50), nullable=False)
    scenario_code: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("core.enterprises.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    origin_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    origin_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    destination_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    destination_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    warehouse_inbound_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warehouse_inbound_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warehouse_outbound_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warehouse_outbound_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    volume_m3: Mapped[float] = mapped_column(Float, nullable=False)
    temperature_zone: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False, index=True)


class AlgorithmRun(Base):
    __tablename__ = "algorithm_runs"
    __table_args__ = (
        UniqueConstraint(
            "algorithm_type",
            "showcase_key",
            "showcase_batch_key",
            "input_signature",
            name="uq_b01_algorithm_showcase_signature",
        ),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    algorithm_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    scenario_code: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    rules_version: Mapped[str] = mapped_column(String(30), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    output_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    showcase_key: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    showcase_batch_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    showcase_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    showcase_source_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TransportPlan(Base, TimestampVersionMixin):
    __tablename__ = "transport_plans"
    __table_args__ = (
        UniqueConstraint("plan_no", name="uq_b01_transport_plans_no"),
        UniqueConstraint("algorithm_run_id", name="uq_b01_transport_plans_algorithm_run"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_no: Mapped[str] = mapped_column(String(50), nullable=False)
    algorithm_run_id: Mapped[str] = mapped_column(ForeignKey("b01.algorithm_runs.id"), nullable=False, index=True)
    candidate_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="CONFIRMED", nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    published_qr_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    qr_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class DashboardProjection(Base, TimestampVersionMixin):
    __tablename__ = "dashboard_projections"
    __table_args__ = ({"schema": "b01"},)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default="current")
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DashboardMapPoint(Base, TimestampVersionMixin):
    __tablename__ = "dashboard_map_points"
    __table_args__ = (
        UniqueConstraint("point_code", name="uq_b01_dashboard_map_point_code"),
        CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_b01_map_point_longitude"),
        CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_b01_map_point_latitude"),
        Index("ix_b01_dashboard_map_point_order", "enabled", "featured", "display_order"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    point_code: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(ForeignKey("core.stores.id"), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    point_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    brand_name: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str] = mapped_column(String(240), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), nullable=False)
    latitude: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), nullable=False)
    coordinate_crs: Mapped[str] = mapped_column(String(20), default="EPSG:4326", nullable=False)
    coordinate_accuracy: Mapped[str] = mapped_column(String(30), nullable=False)
    source_longitude: Mapped[float | None] = mapped_column(Numeric(10, 6, asdecimal=False), nullable=True)
    source_latitude: Mapped[float | None] = mapped_column(Numeric(10, 6, asdecimal=False), nullable=True)
    source_crs: Mapped[str] = mapped_column(String(20), nullable=False)
    conversion_method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    featured: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    catalog_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WarehousePoolPlan(Base, TimestampVersionMixin):
    __tablename__ = "warehouse_pool_plans"
    __table_args__ = (
        UniqueConstraint("plan_no", name="uq_b01_warehouse_pool_plans_no"),
        UniqueConstraint("algorithm_run_id", name="uq_b01_warehouse_pool_plans_run"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_no: Mapped[str] = mapped_column(String(50), nullable=False)
    algorithm_run_id: Mapped[str] = mapped_column(ForeignKey("b01.algorithm_runs.id"), nullable=False, index=True)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("core.warehouses.id"), nullable=False, index=True)
    order_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    required_volume_m3: Mapped[float] = mapped_column(Float, nullable=False)
    inbound_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inbound_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outbound_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outbound_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RESERVATION_REQUESTED", nullable=False, index=True)
    reservation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class WarehouseReservation(Base, TimestampVersionMixin):
    __tablename__ = "warehouse_reservations"
    __table_args__ = (
        UniqueConstraint("warehouse_pool_plan_id", name="uq_b01_warehouse_reservations_plan"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    warehouse_pool_plan_id: Mapped[str] = mapped_column(
        ForeignKey("b01.warehouse_pool_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("core.warehouses.id"), nullable=False, index=True)
    reserved_volume_m3: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RESERVED", nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    occupied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReceiptProjection(Base):
    __tablename__ = "receipt_projections"
    __table_args__ = (
        UniqueConstraint("source_receipt_id", name="uq_b01_receipt_projection_source"),
        UniqueConstraint("source_event_id", name="uq_b01_receipt_projection_event"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_receipt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    receipt_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    expected_total: Mapped[float] = mapped_column(Float, nullable=False)
    received_total: Mapped[float] = mapped_column(Float, nullable=False)
    difference_total: Mapped[float] = mapped_column(Float, nullable=False)
    rejected_line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    business_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InventoryMovementProjection(Base):
    __tablename__ = "inventory_movement_projections"
    __table_args__ = (
        UniqueConstraint("source_movement_id", name="uq_b01_inventory_movement_projection_source"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_movement_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    quantity_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity_delta: Mapped[float] = mapped_column(Float, nullable=False)
    quantity_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity_context: Mapped[str] = mapped_column(String(40), default="RECORDED", nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    business_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StockoutDemandProjection(Base):
    __tablename__ = "stockout_demand_projections"
    __table_args__ = (
        UniqueConstraint("source_stockout_id", name="uq_b01_stockout_projection_source"),
        UniqueConstraint("source_event_id", name="uq_b01_stockout_projection_event"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_stockout_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    requested_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    business_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StoreDailyReportProjection(Base):
    __tablename__ = "store_daily_report_projections"
    __table_args__ = (
        UniqueConstraint("source_report_id", name="uq_b01_daily_report_projection_source"),
        UniqueConstraint("source_event_id", name="uq_b01_daily_report_projection_event"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sales_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    authorized_for_dashboard: Mapped[bool] = mapped_column(Boolean, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    business_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TelemetryIssueProjection(Base):
    __tablename__ = "telemetry_issue_projections"
    __table_args__ = (
        UniqueConstraint("source_issue_id", name="uq_b01_telemetry_issue_projection_source"),
        UniqueConstraint("source_event_id", name="uq_b01_telemetry_issue_projection_event"),
        Index("ix_b01_telemetry_issue_business", "business_at"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_issue_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    store_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    task_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    expected_vehicle_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actual_vehicle_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expected_driver_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actual_driver_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    business_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DemandHistory(Base):
    __tablename__ = "demand_history"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "product_id", "period_start", name="uq_b01_demand_period"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("core.enterprises.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)


class ProductionPlan(Base):
    __tablename__ = "production_plans"
    __table_args__ = ({"schema": "b01"},)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("core.enterprises.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    plan_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    planned_quantity: Mapped[float] = mapped_column(Float, nullable=False)


class ProcurementDemandConfirmation(Base, TimestampVersionMixin):
    __tablename__ = "procurement_demand_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "enterprise_id",
            "product_id",
            "cycle_start",
            name="uq_b01_procurement_demand_enterprise_product_cycle",
        ),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("core.enterprises.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    cycle_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    cycle_end: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="CONFIRMED", nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    confirmed_by: Mapped[str] = mapped_column(ForeignKey("iam.users.id"), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProcurementAggregation(Base, TimestampVersionMixin):
    __tablename__ = "procurement_aggregations"
    __table_args__ = (
        UniqueConstraint("product_id", "cycle_start", name="uq_b01_procurement_aggregation_product_cycle"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    cycle_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    cycle_end: Mapped[date] = mapped_column(Date, nullable=False)
    base_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    automatic_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    adjusted_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False, index=True)
    rules_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    candidate_snapshot: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    recommendation_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    unit_conversion_warnings: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    adjustment_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("iam.users.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DemandForecastProjection(Base, TimestampVersionMixin):
    __tablename__ = "demand_forecast_projections"
    __table_args__ = (
        UniqueConstraint(
            "enterprise_id",
            "product_id",
            "forecast_start",
            name="uq_b01_forecast_enterprise_product_period",
        ),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("core.enterprises.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    forecast_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    forecast_end: Mapped[date] = mapped_column(Date, nullable=False)
    historical_usage: Mapped[float] = mapped_column(Float, nullable=False)
    production_plan_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    forecast_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggested_purchase_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    lower_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)
    smape: Mapped[float | None] = mapped_column(Float, nullable=True)
    method: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    method_note: Mapped[str] = mapped_column(String(500), nullable=False)
    data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)


class DemandForecastBatch(Base, TimestampVersionMixin):
    __tablename__ = "demand_forecast_batches"
    __table_args__ = (
        UniqueConstraint("forecast_start", name="uq_b01_forecast_batch_period"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    forecast_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    forecast_end: Mapped[date] = mapped_column(Date, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(40), nullable=False)
    data_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_snapshot: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    data_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="READY", nullable=False, index=True)
    scheduled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TransportTask(Base, TimestampVersionMixin):
    __tablename__ = "transport_tasks"
    __table_args__ = (UniqueConstraint("task_no", name="uq_b02_transport_tasks_no"), {"schema": "b02"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_no: Mapped[str] = mapped_column(String(50), nullable=False)
    source_order_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("core.vehicles.id"), nullable=False, index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("core.drivers.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="PUBLISHED", nullable=False, index=True)
    temperature_zone: Mapped[str] = mapped_column(String(30), nullable=False)
    total_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    total_volume_m3: Mapped[float] = mapped_column(Float, nullable=False)
    origin_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    origin_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    destination_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    destination_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    planned_departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    qr_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qr_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskStop(Base, TimestampVersionMixin):
    __tablename__ = "task_stops"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence_no", name="uq_b02_task_stop_sequence"),
        UniqueConstraint("task_id", "store_id", name="uq_b02_task_stop_store"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("b02.transport_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PLANNED", nullable=False)
    delivery_lines: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_b02_task_events_idempotency"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("b02.transport_tasks.id"), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(30), nullable=False)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("iam.users.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TaskException(Base):
    __tablename__ = "task_exceptions"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_b02_task_exception_idempotency"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("b02.transport_tasks.id"), nullable=False, index=True)
    exception_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    original_status: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("iam.users.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("task_id", "store_id", name="uq_b02_receipt_task_store"),
        UniqueConstraint("task_id", "idempotency_key", name="uq_b02_receipts_idempotency"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("b02.transport_tasks.id"), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    receipt_status: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("iam.users.id"), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InventoryBalance(Base, TimestampVersionMixin):
    __tablename__ = "inventory_balances"
    __table_args__ = (
        UniqueConstraint("store_id", "product_id", name="uq_b02_inventory_store_product"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    low_stock_threshold: Mapped[float] = mapped_column(Float, default=20, nullable=False)


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        UniqueConstraint("store_id", "idempotency_key", name="uq_b02_inventory_movements_idempotency"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity_delta: Mapped[float] = mapped_column(Float, nullable=False)
    quantity_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity_context: Mapped[str] = mapped_column(String(40), default="RECORDED", nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StockoutRequest(Base, TimestampVersionMixin):
    __tablename__ = "stockout_requests"
    __table_args__ = (
        UniqueConstraint("store_id", "idempotency_key", name="uq_b02_stockout_idempotency"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("core.products.id"), nullable=False, index=True)
    requested_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="SUBMITTED", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)


class StoreDailyReport(Base, TimestampVersionMixin):
    __tablename__ = "store_daily_reports"
    __table_args__ = (
        UniqueConstraint("store_id", "report_date", name="uq_b02_store_daily_date"),
        UniqueConstraint("store_id", "idempotency_key", name="uq_b02_store_daily_idempotency"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("core.stores.id"), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sales_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    authorized_for_dashboard: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)


class TelemetryPoint(Base):
    __tablename__ = "telemetry_points"
    __table_args__ = (
        Index("ix_b02_telemetry_task_sampled", "task_id", "sampled_at"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("b02.transport_tasks.id"), nullable=False, index=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("core.vehicles.id"), nullable=False, index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    temperature_c: Mapped[float] = mapped_column(Float, nullable=False)
    humidity_pct: Mapped[float] = mapped_column(Float, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    anomaly_code: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)


class TelemetryIssue(Base):
    __tablename__ = "telemetry_issues"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_b02_telemetry_issue_source_key"),
        Index("ix_b02_telemetry_issue_task_occurred", "task_id", "occurred_at"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_key: Mapped[str] = mapped_column(String(240), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    store_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False, index=True)
    task_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    expected_vehicle_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actual_vehicle_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expected_driver_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actual_driver_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LocationPoint(Base):
    __tablename__ = "location_points"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_b02_location_idempotency"),
        Index("ix_b02_location_task_recorded", "task_id", "recorded_at"),
        {"schema": "b02"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("b02.transport_tasks.id"), nullable=False, index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("core.drivers.id"), nullable=False, index=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    speed_mps: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = ({"schema": "b02"},)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("iam.users.id"), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    original_name: Mapped[str] = mapped_column(String(240), nullable=False)
    storage_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Alert(Base, TimestampVersionMixin):
    __tablename__ = "alerts"
    __table_args__ = ({"schema": "b02"},)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("b02.transport_tasks.id"), nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(240), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("iam.users.id"), nullable=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_integration_idempotency_scope_key"),
        {"schema": "integration"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="PROCESSING", nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceQuotaBucket(Base):
    __tablename__ = "voice_quota_buckets"
    __table_args__ = (
        UniqueConstraint(
            "subject_hash",
            "scope",
            "window_started_at",
            name="uq_b01_voice_quota_window",
        ),
        Index("ix_b01_voice_quota_scope_window", "scope", "window_started_at"),
        CheckConstraint("window_seconds > 0", name="ck_b01_voice_quota_window_positive"),
        CheckConstraint("request_count >= 0", name="ck_b01_voice_quota_count_nonnegative"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(40), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VoiceConcurrencyLease(Base):
    __tablename__ = "voice_concurrency_leases"
    __table_args__ = (
        UniqueConstraint(
            "subject_hash",
            "client_request_id",
            name="uq_b01_voice_lease_subject_request",
        ),
        Index("ix_b01_voice_lease_expires", "expires_at"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VoiceTranscriptionRequest(Base):
    __tablename__ = "voice_transcription_requests"
    __table_args__ = (
        UniqueConstraint(
            "request_scope",
            "subject_hash",
            "idempotency_key",
            name="uq_b01_voice_transcription_subject_key",
        ),
        Index("ix_b01_voice_transcription_state_expiry", "state", "replay_expires_at"),
        CheckConstraint(
            "state IN ('PROCESSING', 'COMPLETED', 'FAILED')",
            name="ck_b01_voice_transcription_state",
        ),
        CheckConstraint("duration_seconds > 0", name="ck_b01_voice_transcription_duration_positive"),
        {"schema": "b01"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="PROCESSING", nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replay_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_integration_outbox_status_sequence", "status", "sequence"), {"schema": "integration"})

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), default=new_id, unique=True, nullable=False)
    topic: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    object_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_integration_audit_object", "object_type", "object_id"), {"schema": "integration"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    before_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DeadLetter(Base):
    __tablename__ = "dead_letters"
    __table_args__ = ({"schema": "integration"},)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
