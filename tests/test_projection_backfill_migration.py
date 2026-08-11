from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260808_0011_backfill_business_projections.py"
    )
    spec = importlib.util.spec_from_file_location("projection_backfill_0011", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0011_tables(engine: sa.Engine) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    tables = {
        "receipts": sa.Table(
            "receipts",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("task_id", sa.String(36), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("receipt_status", sa.String(30), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "balances": sa.Table(
            "inventory_balances",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("product_id", sa.String(36), nullable=False),
            sa.Column("quantity", sa.Float(), nullable=False),
        ),
        "movements": sa.Table(
            "inventory_movements",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("product_id", sa.String(36), nullable=False),
            sa.Column("movement_type", sa.String(30), nullable=False),
            sa.Column("quantity_before", sa.Float(), nullable=False),
            sa.Column("quantity_delta", sa.Float(), nullable=False),
            sa.Column("quantity_after", sa.Float(), nullable=False),
            sa.Column("source_type", sa.String(30), nullable=False),
            sa.Column("source_id", sa.String(36), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "stockouts": sa.Table(
            "stockout_requests",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("product_id", sa.String(36), nullable=False),
            sa.Column("requested_quantity", sa.Float(), nullable=False),
            sa.Column("reason", sa.String(240), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("object_version", sa.Integer(), nullable=False),
        ),
        "reports": sa.Table(
            "store_daily_reports",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("report_date", sa.Date(), nullable=False),
            sa.Column("sales_amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("order_count", sa.Integer(), nullable=False),
            sa.Column("authorized_for_dashboard", sa.Boolean(), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("object_version", sa.Integer(), nullable=False),
        ),
        "receipt_projections": sa.Table(
            "receipt_projections",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("source_receipt_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_event_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_version", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.String(36), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("receipt_status", sa.String(30), nullable=False),
            sa.Column("expected_total", sa.Float(), nullable=False),
            sa.Column("received_total", sa.Float(), nullable=False),
            sa.Column("difference_total", sa.Float(), nullable=False),
            sa.Column("rejected_line_count", sa.Integer(), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("business_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "movement_projections": sa.Table(
            "inventory_movement_projections",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("source_movement_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_event_id", sa.String(36), nullable=False),
            sa.Column("source_version", sa.Integer(), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("product_id", sa.String(36), nullable=False),
            sa.Column("movement_type", sa.String(30), nullable=False),
            sa.Column("quantity_before", sa.Float(), nullable=False),
            sa.Column("quantity_delta", sa.Float(), nullable=False),
            sa.Column("quantity_after", sa.Float(), nullable=False),
            sa.Column("source_type", sa.String(30), nullable=False),
            sa.Column("source_id", sa.String(36), nullable=False),
            sa.Column("business_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "stockout_projections": sa.Table(
            "stockout_demand_projections",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("source_stockout_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_event_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_version", sa.Integer(), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("product_id", sa.String(36), nullable=False),
            sa.Column("requested_quantity", sa.Float(), nullable=False),
            sa.Column("reason", sa.String(240), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("business_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        ),
        "report_projections": sa.Table(
            "store_daily_report_projections",
            metadata,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("source_report_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_event_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_version", sa.Integer(), nullable=False),
            sa.Column("store_id", sa.String(36), nullable=False),
            sa.Column("report_date", sa.Date(), nullable=False),
            sa.Column("sales_amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("order_count", sa.Integer(), nullable=False),
            sa.Column("authorized_for_dashboard", sa.Boolean(), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=False),
            sa.Column("business_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        ),
    }
    metadata.create_all(engine)
    return tables


def test_0011_backfills_operations_idempotently_without_overwriting_existing_projection():
    migration = _load_migration()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    tables = _pre_0011_tables(engine)
    received_at = datetime(2026, 8, 5, 8, 30, tzinfo=UTC)
    movement_at = received_at + timedelta(minutes=5)
    stockout_updated_at = received_at + timedelta(hours=1)
    report_updated_at = received_at + timedelta(hours=2)

    with engine.begin() as connection:
        connection.execute(
            tables["receipts"].insert(),
            {
                "id": "receipt-1",
                "task_id": "task-1",
                "store_id": "store-1",
                "receipt_status": "PARTIAL",
                "details": [
                    {
                        "product_id": "product-1",
                        "expected_quantity": 12.0,
                        "received_quantity": 9.0,
                    }
                ],
                "received_at": received_at,
            },
        )
        connection.execute(
            tables["balances"].insert(),
            {"id": "balance-1", "store_id": "store-1", "product_id": "product-1", "quantity": 25.0},
        )
        connection.execute(
            tables["movements"].insert(),
            [
                {
                    "id": "movement-1",
                    "store_id": "store-1",
                    "product_id": "product-1",
                    "movement_type": "IN",
                    "quantity_before": 0.0,
                    "quantity_delta": 9.0,
                    "quantity_after": 0.0,
                    "source_type": "RECEIPT",
                    "source_id": "receipt-1",
                    "occurred_at": movement_at,
                },
                {
                    "id": "movement-2",
                    "store_id": "store-1",
                    "product_id": "product-1",
                    "movement_type": "OUT",
                    "quantity_before": 0.0,
                    "quantity_delta": -4.0,
                    "quantity_after": 0.0,
                    "source_type": "ADJUSTMENT",
                    "source_id": "adjustment-1",
                    "occurred_at": movement_at + timedelta(minutes=1),
                },
                {
                    "id": "movement-without-balance",
                    "store_id": "store-2",
                    "product_id": "product-2",
                    "movement_type": "IN",
                    "quantity_before": 0.0,
                    "quantity_delta": 5.0,
                    "quantity_after": 0.0,
                    "source_type": "RECEIPT",
                    "source_id": "receipt-2",
                    "occurred_at": movement_at,
                },
            ],
        )
        connection.execute(
            tables["stockouts"].insert(),
            {
                "id": "stockout-1",
                "store_id": "store-1",
                "product_id": "product-1",
                "requested_quantity": 18.0,
                "reason": "当前来源原因",
                "status": "SUBMITTED",
                "updated_at": stockout_updated_at,
                "object_version": 3,
            },
        )
        connection.execute(
            tables["reports"].insert(),
            {
                "id": "report-1",
                "store_id": "store-1",
                "report_date": date(2026, 8, 4),
                "sales_amount": Decimal("5680.50"),
                "order_count": 73,
                "authorized_for_dashboard": True,
                "summary": {"source": "historical"},
                "updated_at": report_updated_at,
                "object_version": 2,
            },
        )
        connection.execute(
            tables["stockout_projections"].insert(),
            {
                "id": "existing-stockout-projection",
                "source_stockout_id": "stockout-1",
                "source_event_id": "existing-stockout-event",
                "source_version": 99,
                "store_id": "store-1",
                "product_id": "product-1",
                "requested_quantity": 7.0,
                "reason": "已有投影必须保留",
                "status": "FULFILLED",
                "business_at": received_at,
                "projected_at": received_at,
            },
        )
        connection.execute(
            tables["movement_projections"].insert(),
            {
                "id": "existing-movement-projection",
                "source_movement_id": "movement-2",
                "source_event_id": "existing-movement-event",
                "source_version": 5,
                "store_id": "store-1",
                "product_id": "product-1",
                "movement_type": "OUT",
                "quantity_before": 777.0,
                "quantity_delta": -4.0,
                "quantity_after": 773.0,
                "source_type": "ADJUSTMENT",
                "source_id": "adjustment-1",
                "business_at": movement_at,
                "projected_at": movement_at,
            },
        )

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        migration._backfill_existing_operations(connection)

        reflected = sa.MetaData()
        movements = sa.Table("inventory_movements", reflected, autoload_with=connection)
        movement_projections = sa.Table(
            "inventory_movement_projections",
            reflected,
            autoload_with=connection,
        )
        receipt_projections = sa.Table("receipt_projections", reflected, autoload_with=connection)
        stockout_projections = sa.Table("stockout_demand_projections", reflected, autoload_with=connection)
        report_projections = sa.Table("store_daily_report_projections", reflected, autoload_with=connection)

        source_movements = {
            row["id"]: row
            for row in connection.execute(sa.select(movements)).mappings()
        }
        assert source_movements["movement-1"]["quantity_before"] == 20.0
        assert source_movements["movement-1"]["quantity_after"] == 29.0
        assert source_movements["movement-2"]["quantity_before"] == 29.0
        assert source_movements["movement-2"]["quantity_after"] == 25.0
        assert source_movements["movement-1"]["quantity_context"] == "RECONSTRUCTED_FROM_BALANCE"
        assert source_movements["movement-without-balance"]["quantity_before"] is None
        assert source_movements["movement-without-balance"]["quantity_after"] is None
        assert source_movements["movement-without-balance"]["quantity_context"] == "UNKNOWN_NO_BALANCE"

        receipt_projection = connection.execute(sa.select(receipt_projections)).mappings().one()
        assert receipt_projection["expected_total"] == 12.0
        assert receipt_projection["received_total"] == 9.0
        assert receipt_projection["difference_total"] == 3.0
        assert receipt_projection["business_at"] == received_at.replace(tzinfo=None)

        projected_movements = {
            row["source_movement_id"]: row
            for row in connection.execute(sa.select(movement_projections)).mappings()
        }
        assert len(projected_movements) == 3
        assert projected_movements["movement-1"]["quantity_before"] == 20.0
        assert projected_movements["movement-1"]["quantity_context"] == "RECONSTRUCTED_FROM_BALANCE"
        assert projected_movements["movement-2"]["quantity_before"] == 777.0
        assert projected_movements["movement-2"]["quantity_context"] == "UNKNOWN"
        assert projected_movements["movement-without-balance"]["quantity_before"] is None

        existing_stockout = connection.execute(sa.select(stockout_projections)).mappings().one()
        assert existing_stockout["source_version"] == 99
        assert existing_stockout["reason"] == "已有投影必须保留"
        assert existing_stockout["status"] == "FULFILLED"

        report_projection = connection.execute(sa.select(report_projections)).mappings().one()
        assert report_projection["source_version"] == 2
        assert report_projection["business_at"] == report_updated_at.replace(tzinfo=None)
        assert report_projection["business_at"] != report_projection["projected_at"]

        connection.execute(
            movements.insert(),
            {
                "id": "future-movement",
                "store_id": "store-1",
                "product_id": "product-1",
                "movement_type": "IN",
                "quantity_before": 25.0,
                "quantity_delta": 2.0,
                "quantity_after": 27.0,
                "source_type": "RECEIPT",
                "source_id": "receipt-future",
                "occurred_at": report_updated_at,
            },
        )
        future_context = connection.scalar(
            sa.select(movements.c.quantity_context).where(movements.c.id == "future-movement")
        )
        assert future_context == "RECORDED"


def test_0011_refuses_to_invent_receipt_totals_from_invalid_details():
    migration = _load_migration()
    with pytest.raises(RuntimeError, match="拒绝伪造经营投影"):
        migration._receipt_totals(
            "receipt-invalid",
            [{"expected_quantity": 12.0, "received_quantity": "not-a-number"}],
            "PARTIAL",
        )
