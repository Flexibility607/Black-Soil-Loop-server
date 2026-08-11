"""Backfill existing B02 operations into the B01 business projections.

Revision ID: 20260808_0011
Revises: 20260808_0010
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from math import fsum, isclose, isfinite
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

revision = "20260808_0011"
down_revision = "20260808_0010"
branch_labels = None
depends_on = None

BACKFILL_NAMESPACE = UUID("f5a78657-f2ba-4f62-9d14-bc95995eeacf")
UNKNOWN_CONTEXT = "UNKNOWN"
RECORDED_CONTEXT = "RECORDED"
RECONSTRUCTED_CONTEXT = "RECONSTRUCTED_FROM_BALANCE"
UNKNOWN_NO_BALANCE_CONTEXT = "UNKNOWN_NO_BALANCE"
UNKNOWN_INCONSISTENT_CONTEXT = "UNKNOWN_INCONSISTENT_HISTORY"


def _schema(name: str) -> str | None:
    return None if op.get_bind().dialect.name == "sqlite" else name


def _columns(schema: str, table: str) -> dict[str, dict[str, Any]]:
    bind = op.get_bind()
    database_schema = None if bind.dialect.name == "sqlite" else schema
    return {
        column["name"]: column
        for column in sa.inspect(bind).get_columns(table, schema=database_schema)
    }


def _ensure_inventory_semantics_columns() -> None:
    for schema_name, table_name in (
        ("b02", "inventory_movements"),
        ("b01", "inventory_movement_projections"),
    ):
        schema = _schema(schema_name)
        columns = _columns(schema_name, table_name)
        if "quantity_context" not in columns:
            op.add_column(
                table_name,
                sa.Column(
                    "quantity_context",
                    sa.String(length=40),
                    nullable=False,
                    server_default=UNKNOWN_CONTEXT,
                ),
                schema=schema,
            )
            columns = _columns(schema_name, table_name)
        nullable_fields = [
            name
            for name in ("quantity_before", "quantity_after")
            if not columns[name]["nullable"]
        ]
        if nullable_fields:
            with op.batch_alter_table(table_name, schema=schema) as batch_op:
                for name in nullable_fields:
                    batch_op.alter_column(
                        name,
                        existing_type=sa.Float(),
                        existing_nullable=False,
                        nullable=True,
                    )


def _set_recorded_server_defaults() -> None:
    for schema_name, table_name in (
        ("b02", "inventory_movements"),
        ("b01", "inventory_movement_projections"),
    ):
        with op.batch_alter_table(table_name, schema=_schema(schema_name)) as batch_op:
            batch_op.alter_column(
                "quantity_context",
                existing_type=sa.String(length=40),
                existing_nullable=False,
                server_default=RECORDED_CONTEXT,
            )


def _table(bind: sa.Connection, schema: str, name: str) -> sa.Table:
    database_schema = None if bind.dialect.name == "sqlite" else schema
    return sa.Table(name, sa.MetaData(), schema=database_schema, autoload_with=bind)


def _synthetic_id(kind: str, source_id: str) -> str:
    return str(uuid5(BACKFILL_NAMESPACE, f"{kind}:{source_id}"))


def _insert_do_nothing(bind: sa.Connection, table: sa.Table, values: dict[str, Any]) -> None:
    if bind.dialect.name == "postgresql":
        statement = postgresql_insert(table).values(values).on_conflict_do_nothing()
    elif bind.dialect.name == "sqlite":
        statement = sqlite_insert(table).values(values).on_conflict_do_nothing()
    else:
        raise RuntimeError(f"经营投影历史回填不支持数据库方言 {bind.dialect.name}")
    bind.execute(statement)


def _as_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _values_match(left: Any, right: float) -> bool:
    value = _as_finite_float(left)
    return value is not None and isclose(value, right, rel_tol=1e-9, abs_tol=1e-9)


def _stored_history_is_consistent(rows: list[dict[str, Any]]) -> bool:
    previous_after: float | None = None
    for row in rows:
        before = _as_finite_float(row["quantity_before"])
        delta = _as_finite_float(row["quantity_delta"])
        after = _as_finite_float(row["quantity_after"])
        if before is None or delta is None or after is None:
            return False
        if not isclose(after, before + delta, rel_tol=1e-9, abs_tol=1e-9):
            return False
        if previous_after is not None and not isclose(
            before,
            previous_after,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            return False
        previous_after = after
    return True


def _update_unknown_movement(
    bind: sa.Connection,
    movements: sa.Table,
    movement_id: str,
    *,
    quantity_before: float | None,
    quantity_after: float | None,
    quantity_context: str,
) -> None:
    bind.execute(
        sa.update(movements)
        .where(
            movements.c.id == movement_id,
            movements.c.quantity_context == UNKNOWN_CONTEXT,
        )
        .values(
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            quantity_context=quantity_context,
        )
    )


def _repair_inventory_history(
    bind: sa.Connection,
    movements: sa.Table,
    balances: sa.Table,
) -> None:
    rows = list(
        bind.execute(
            sa.select(movements).order_by(
                movements.c.store_id,
                movements.c.product_id,
                movements.c.occurred_at,
                movements.c.id,
            )
        ).mappings()
    )
    balance_by_key = {
        (str(row["store_id"]), str(row["product_id"])): _as_finite_float(row["quantity"])
        for row in bind.execute(sa.select(balances)).mappings()
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw_row in rows:
        row = dict(raw_row)
        grouped[(str(row["store_id"]), str(row["product_id"]))].append(row)

    for key, group in grouped.items():
        unknown_rows = [row for row in group if row["quantity_context"] == UNKNOWN_CONTEXT]
        if not unknown_rows:
            continue
        balance = balance_by_key.get(key)
        deltas = [_as_finite_float(row["quantity_delta"]) for row in group]
        if balance is not None and all(delta is not None for delta in deltas):
            running = balance - fsum(delta for delta in deltas if delta is not None)
            candidates: dict[str, tuple[float, float]] = {}
            for row, delta in zip(group, deltas, strict=True):
                assert delta is not None
                before = running
                after = before + delta
                candidates[str(row["id"])] = (before, after)
                running = after
            explicit_rows_match = all(
                row["quantity_context"] == UNKNOWN_CONTEXT
                or (
                    _values_match(row["quantity_before"], candidates[str(row["id"])][0])
                    and _values_match(row["quantity_after"], candidates[str(row["id"])][1])
                )
                for row in group
            )
            if explicit_rows_match:
                for row in unknown_rows:
                    before, after = candidates[str(row["id"])]
                    _update_unknown_movement(
                        bind,
                        movements,
                        str(row["id"]),
                        quantity_before=before,
                        quantity_after=after,
                        quantity_context=RECONSTRUCTED_CONTEXT,
                    )
                continue
            unknown_context = UNKNOWN_INCONSISTENT_CONTEXT
        elif _stored_history_is_consistent(group):
            for row in unknown_rows:
                _update_unknown_movement(
                    bind,
                    movements,
                    str(row["id"]),
                    quantity_before=float(row["quantity_before"]),
                    quantity_after=float(row["quantity_after"]),
                    quantity_context=RECORDED_CONTEXT,
                )
            continue
        else:
            unknown_context = UNKNOWN_NO_BALANCE_CONTEXT if balance is None else UNKNOWN_INCONSISTENT_CONTEXT

        for row in unknown_rows:
            _update_unknown_movement(
                bind,
                movements,
                str(row["id"]),
                quantity_before=None,
                quantity_after=None,
                quantity_context=unknown_context,
            )


def _receipt_totals(source_id: str, details: Any, status: str) -> tuple[float, float, int]:
    if not isinstance(details, list):
        raise RuntimeError(f"签收 {source_id} 的 details 不是明细数组，拒绝伪造经营投影")
    expected_total = 0.0
    received_total = 0.0
    rejected_line_count = 0
    for line in details:
        if not isinstance(line, dict):
            raise RuntimeError(f"签收 {source_id} 包含非法明细，拒绝伪造经营投影")
        expected = _as_finite_float(line.get("expected_quantity"))
        received = _as_finite_float(line.get("received_quantity"))
        if expected is None or received is None or expected < 0 or received < 0:
            raise RuntimeError(f"签收 {source_id} 包含非法数量，拒绝伪造经营投影")
        expected_total += expected
        received_total += received
        if status == "REJECTED" or received <= 0:
            rejected_line_count += 1
    return expected_total, received_total, rejected_line_count


def _backfill_existing_operations(bind: sa.Connection) -> None:
    receipts = _table(bind, "b02", "receipts")
    movements = _table(bind, "b02", "inventory_movements")
    balances = _table(bind, "b02", "inventory_balances")
    stockouts = _table(bind, "b02", "stockout_requests")
    reports = _table(bind, "b02", "store_daily_reports")
    receipt_projections = _table(bind, "b01", "receipt_projections")
    movement_projections = _table(bind, "b01", "inventory_movement_projections")
    stockout_projections = _table(bind, "b01", "stockout_demand_projections")
    report_projections = _table(bind, "b01", "store_daily_report_projections")

    _repair_inventory_history(bind, movements, balances)
    projected_at = datetime.now(UTC)

    for source in bind.execute(sa.select(receipts).order_by(receipts.c.id)).mappings():
        source_id = str(source["id"])
        expected_total, received_total, rejected_line_count = _receipt_totals(
            source_id,
            source["details"],
            str(source["receipt_status"]),
        )
        _insert_do_nothing(
            bind,
            receipt_projections,
            {
                "id": _synthetic_id("receipt-projection", source_id),
                "source_receipt_id": source_id,
                "source_event_id": _synthetic_id("receipt-backfill-event", source_id),
                "source_version": 1,
                "task_id": source["task_id"],
                "store_id": source["store_id"],
                "receipt_status": source["receipt_status"],
                "expected_total": expected_total,
                "received_total": received_total,
                "difference_total": max(expected_total - received_total, 0.0),
                "rejected_line_count": rejected_line_count,
                "details": source["details"],
                "business_at": source["received_at"],
                "projected_at": projected_at,
            },
        )

    repaired_movements = bind.execute(sa.select(movements).order_by(movements.c.id)).mappings()
    for source in repaired_movements:
        source_id = str(source["id"])
        _insert_do_nothing(
            bind,
            movement_projections,
            {
                "id": _synthetic_id("inventory-movement-projection", source_id),
                "source_movement_id": source_id,
                "source_event_id": _synthetic_id("inventory-movement-backfill-event", source_id),
                "source_version": 1,
                "store_id": source["store_id"],
                "product_id": source["product_id"],
                "movement_type": source["movement_type"],
                "quantity_before": source["quantity_before"],
                "quantity_delta": source["quantity_delta"],
                "quantity_after": source["quantity_after"],
                "quantity_context": source["quantity_context"],
                "source_type": source["source_type"],
                "source_id": source["source_id"],
                "business_at": source["occurred_at"],
                "projected_at": projected_at,
            },
        )

    for source in bind.execute(sa.select(stockouts).order_by(stockouts.c.id)).mappings():
        source_id = str(source["id"])
        _insert_do_nothing(
            bind,
            stockout_projections,
            {
                "id": _synthetic_id("stockout-projection", source_id),
                "source_stockout_id": source_id,
                "source_event_id": _synthetic_id("stockout-backfill-event", source_id),
                "source_version": source["object_version"],
                "store_id": source["store_id"],
                "product_id": source["product_id"],
                "requested_quantity": source["requested_quantity"],
                "reason": source["reason"],
                "status": source["status"],
                "business_at": source["updated_at"],
                "projected_at": projected_at,
            },
        )

    for source in bind.execute(sa.select(reports).order_by(reports.c.id)).mappings():
        source_id = str(source["id"])
        _insert_do_nothing(
            bind,
            report_projections,
            {
                "id": _synthetic_id("daily-report-projection", source_id),
                "source_report_id": source_id,
                "source_event_id": _synthetic_id("daily-report-backfill-event", source_id),
                "source_version": source["object_version"],
                "store_id": source["store_id"],
                "report_date": source["report_date"],
                "sales_amount": source["sales_amount"],
                "order_count": source["order_count"],
                "authorized_for_dashboard": source["authorized_for_dashboard"],
                "summary": source["summary"],
                "business_at": source["updated_at"],
                "projected_at": projected_at,
            },
        )


def upgrade() -> None:
    _ensure_inventory_semantics_columns()
    _backfill_existing_operations(op.get_bind())
    _set_recorded_server_defaults()


def downgrade() -> None:
    raise RuntimeError("经营投影历史回填包含不可逆的数据语义修复，正式环境只允许向前迁移")
