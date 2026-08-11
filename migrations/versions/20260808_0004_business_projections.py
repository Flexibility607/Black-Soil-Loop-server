"""Add B01 receipt, inventory, stockout, and daily report projections.

Revision ID: 20260808_0004
Revises: 20260808_0003
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

from app.shared.models import (
    InventoryMovementProjection,
    ReceiptProjection,
    StockoutDemandProjection,
    StoreDailyReportProjection,
)

revision = "20260808_0004"
down_revision = "20260808_0003"
branch_labels = None
depends_on = None


def _columns(schema: str, table: str) -> set[str]:
    bind = op.get_bind()
    database_schema = None if bind.dialect.name == "sqlite" else schema
    return {
        column["name"]
        for column in sa.inspect(bind).get_columns(table, schema=database_schema)
    }


def upgrade() -> None:
    bind = op.get_bind()
    schema = None if bind.dialect.name == "sqlite" else "b02"
    movement_columns = _columns("b02", "inventory_movements")
    for name in ("quantity_before", "quantity_after"):
        if name not in movement_columns:
            op.add_column(
                "inventory_movements",
                sa.Column(name, sa.Float(), nullable=False, server_default="0"),
                schema=schema,
            )

    for model in (
        ReceiptProjection,
        InventoryMovementProjection,
        StockoutDemandProjection,
        StoreDailyReportProjection,
    ):
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for model in (
        StoreDailyReportProjection,
        StockoutDemandProjection,
        InventoryMovementProjection,
        ReceiptProjection,
    ):
        model.__table__.drop(bind=bind, checkfirst=True)

    schema = None if bind.dialect.name == "sqlite" else "b02"
    movement_columns = _columns("b02", "inventory_movements")
    drop_columns = [
        name for name in ("quantity_after", "quantity_before") if name in movement_columns
    ]
    if drop_columns:
        with op.batch_alter_table("inventory_movements", schema=schema) as batch_op:
            for name in drop_columns:
                batch_op.drop_column(name)
