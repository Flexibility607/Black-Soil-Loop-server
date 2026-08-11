from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(ApiModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


class LoginRequest(ApiModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class WechatLoginRequest(ApiModel):
    code: str = Field(min_length=3, max_length=200)


class RefreshRequest(ApiModel):
    refresh_token: str | None = None


class TokenResponse(ApiModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    access_expires_at: datetime
    refresh_expires_at: datetime
    idle_timeout_seconds: int


class UserResponse(ApiModel):
    id: str
    username: str
    display_name: str
    role: str
    enterprise_id: str | None
    store_id: str | None
    driver_id: str | None
    session_expires_at: datetime | None = None


class TransportOrderCreate(ApiModel):
    order_no: str = Field(min_length=3, max_length=50)
    scenario_code: str = Field(default="MANUAL", min_length=2, max_length=30)
    enterprise_id: str
    product_id: str
    store_id: str
    origin_latitude: float = Field(ge=-90, le=90)
    origin_longitude: float = Field(ge=-180, le=180)
    destination_latitude: float = Field(ge=-90, le=90)
    destination_longitude: float = Field(ge=-180, le=180)
    departure_at: datetime
    warehouse_inbound_start: datetime | None = None
    warehouse_inbound_end: datetime | None = None
    warehouse_outbound_start: datetime | None = None
    warehouse_outbound_end: datetime | None = None
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    weight_kg: float = Field(gt=0)
    volume_m3: float = Field(gt=0)
    temperature_zone: Literal["AMBIENT", "CHILLED", "FROZEN"]

    @model_validator(mode="after")
    def validate_warehouse_windows(self):
        values = (
            self.warehouse_inbound_start,
            self.warehouse_inbound_end,
            self.warehouse_outbound_start,
            self.warehouse_outbound_end,
        )
        if any(value is not None for value in values) and any(value is None for value in values):
            raise ValueError("入仓和出仓时间窗必须同时完整提供")
        if all(value is not None for value in values):
            if self.warehouse_inbound_start >= self.warehouse_inbound_end:
                raise ValueError("入仓时间窗开始时间必须早于结束时间")
            if self.warehouse_outbound_start >= self.warehouse_outbound_end:
                raise ValueError("出仓时间窗开始时间必须早于结束时间")
            if self.warehouse_inbound_end > self.warehouse_outbound_start:
                raise ValueError("入仓时间窗结束时间不能晚于出仓时间窗开始时间")
        return self


class VersionedRequest(ApiModel):
    object_version: int = Field(ge=1)


class CancelRequest(VersionedRequest):
    reason: str = Field(min_length=2, max_length=500)


class MatchPreviewRequest(ApiModel):
    scenario_code: str | None = None
    order_ids: list[str] = Field(default_factory=list)


class MatchConfirmRequest(VersionedRequest):
    match_run_id: str
    candidate_index: int = Field(default=0, ge=0)
    order_versions: dict[str, int] = Field(default_factory=dict)


class WarehousePoolConfirmRequest(ApiModel):
    match_run_id: str
    candidate_index: int = Field(default=0, ge=0)
    warehouse_id: str
    warehouse_object_version: int = Field(ge=1)


class ProcurementDemandConfirmRequest(ApiModel):
    enterprise_id: str
    product_id: str
    cycle_start: date | None = None
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=2, max_length=500)
    object_version: int | None = Field(default=None, ge=1)


class ProcurementGenerateRequest(ApiModel):
    cycle_start: date | None = None


class ProcurementAggregationConfirmRequest(VersionedRequest):
    adjusted_quantity: float | None = Field(default=None, ge=0)
    adjustment_reason: str | None = Field(default=None, min_length=2, max_length=500)

    @model_validator(mode="after")
    def validate_adjustment_reason(self):
        if self.adjusted_quantity is not None and not self.adjustment_reason:
            raise ValueError("手工调整采购量时必须填写调整原因")
        return self


class TaskActionRequest(VersionedRequest):
    action: Literal["ACCEPT", "PICKUP", "START_TRANSIT", "DELIVER"]
    qr_token: str | None = None
    stop_id: str | None = None
    occurred_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)


class ReceiptLine(ApiModel):
    product_id: str
    expected_quantity: float = Field(ge=0)
    received_quantity: float = Field(ge=0)
    note: str | None = Field(default=None, max_length=240)


class ReceiptCreate(VersionedRequest):
    receipt_status: Literal["FULL", "PARTIAL", "REJECTED"]
    qr_token: str
    lines: list[ReceiptLine] = Field(min_length=1)

    @field_validator("lines")
    @classmethod
    def validate_unique_products(cls, lines: list[ReceiptLine]) -> list[ReceiptLine]:
        product_ids = [line.product_id for line in lines]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("签收明细中的商品不能重复")
        return lines


class StockoutCreate(ApiModel):
    product_id: str
    requested_quantity: float = Field(gt=0)
    reason: str = Field(min_length=2, max_length=240)


class DailyReportCreate(ApiModel):
    report_date: date
    sales_amount: Decimal = Field(ge=0)
    order_count: int = Field(ge=0)
    authorized_for_dashboard: bool = True
    summary: dict[str, Any] = Field(default_factory=dict)


class TelemetrySample(ApiModel):
    sampled_at: datetime
    temperature_c: float = Field(ge=-80, le=80)
    humidity_pct: float = Field(ge=0, le=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class TelemetryBatch(ApiModel):
    task_id: str
    vehicle_id: str
    samples: list[TelemetrySample] = Field(min_length=1, max_length=500)


class LocationCreate(ApiModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_mps: float = Field(default=0, ge=0, le=120)
    accuracy_m: float | None = Field(default=None, ge=0, le=10000)
    recorded_at: datetime


class TaskExceptionCreate(ApiModel):
    exception_type: Literal["DELAY", "DAMAGE", "VEHICLE", "TEMPERATURE", "OTHER"]
    reason: str = Field(min_length=2, max_length=500)
    occurred_at: datetime | None = None


class AlertUpdate(VersionedRequest):
    action: Literal["ACKNOWLEDGE", "RESOLVE"]


class AssistantQuery(ApiModel):
    question: str = Field(min_length=2, max_length=500)
    preferred_chart: Literal["bar", "line", "donut", "route", "auto"] = "auto"
