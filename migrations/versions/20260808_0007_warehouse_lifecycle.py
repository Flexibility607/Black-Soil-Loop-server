"""Add shared warehouse reservations and capacity lifecycle.

Revision ID: 20260808_0007
Revises: 20260808_0006
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

from app.shared.models import WarehousePoolPlan, WarehouseReservation

revision = "20260808_0007"
down_revision = "20260808_0006"
branch_labels = None
depends_on = None


def _columns(schema: str, table: str) -> set[str]:
    bind = op.get_bind()
    database_schema = None if bind.dialect.name == "sqlite" else schema
    return {column["name"] for column in sa.inspect(bind).get_columns(table, schema=database_schema)}


def upgrade() -> None:
    bind = op.get_bind()
    core_schema = None if bind.dialect.name == "sqlite" else "core"
    b01_schema = None if bind.dialect.name == "sqlite" else "b01"

    if "reserved_m3" not in _columns("core", "warehouses"):
        op.add_column(
            "warehouses",
            sa.Column("reserved_m3", sa.Float(), nullable=False, server_default="0"),
            schema=core_schema,
        )

    order_columns = _columns("b01", "transport_orders")
    for name in (
        "warehouse_inbound_start",
        "warehouse_inbound_end",
        "warehouse_outbound_start",
        "warehouse_outbound_end",
    ):
        if name not in order_columns:
            op.add_column(
                "transport_orders",
                sa.Column(name, sa.DateTime(timezone=True), nullable=True),
                schema=b01_schema,
            )

    WarehousePoolPlan.__table__.create(bind=bind, checkfirst=True)
    WarehouseReservation.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    WarehouseReservation.__table__.drop(bind=bind, checkfirst=True)
    WarehousePoolPlan.__table__.drop(bind=bind, checkfirst=True)

    core_schema = None if bind.dialect.name == "sqlite" else "core"
    b01_schema = None if bind.dialect.name == "sqlite" else "b01"
    order_columns = _columns("b01", "transport_orders")
    drop_order_columns = [
        name
        for name in (
            "warehouse_outbound_end",
            "warehouse_outbound_start",
            "warehouse_inbound_end",
            "warehouse_inbound_start",
        )
        if name in order_columns
    ]
    if drop_order_columns:
        with op.batch_alter_table("transport_orders", schema=b01_schema) as batch_op:
            for name in drop_order_columns:
                batch_op.drop_column(name)

    if "reserved_m3" in _columns("core", "warehouses"):
        with op.batch_alter_table("warehouses", schema=core_schema) as batch_op:
            batch_op.drop_column("reserved_m3")
