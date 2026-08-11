"""Add atomic idempotency and outbox claim metadata.

Revision ID: 20260808_0003
Revises: 20260807_0002
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision = "20260808_0003"
down_revision = "20260807_0002"
branch_labels = None
depends_on = None


def _columns(schema: str, table: str) -> set[str]:
    bind = op.get_bind()
    database_schema = None if bind.dialect.name == "sqlite" else schema
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table, schema=database_schema)}


def _unique_constraints(schema: str, table: str) -> set[str]:
    bind = op.get_bind()
    database_schema = None if bind.dialect.name == "sqlite" else schema
    inspector = sa.inspect(bind)
    return {
        item["name"]
        for item in inspector.get_unique_constraints(table, schema=database_schema)
        if item["name"]
    }


def upgrade() -> None:
    bind = op.get_bind()
    integration_schema = None if bind.dialect.name == "sqlite" else "integration"
    b01_schema = None if bind.dialect.name == "sqlite" else "b01"
    idempotency_columns = _columns("integration", "idempotency_records")
    state_added = "state" not in idempotency_columns
    if state_added:
        op.add_column(
            "idempotency_records",
            sa.Column("state", sa.String(length=20), nullable=False, server_default="COMPLETED"),
            schema=integration_schema,
        )
        op.create_index(
            "ix_integration_idempotency_records_state",
            "idempotency_records",
            ["state"],
            schema=integration_schema,
        )
    if "completed_at" not in idempotency_columns:
        op.add_column(
            "idempotency_records",
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            schema=integration_schema,
        )
        table_name = "idempotency_records" if integration_schema is None else "integration.idempotency_records"
        op.execute(
            sa.text(
                f"UPDATE {table_name} SET completed_at = created_at "
                "WHERE state = 'COMPLETED' AND completed_at IS NULL"
            )
        )
    if state_added:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("idempotency_records") as batch_op:
                batch_op.alter_column(
                    "state",
                    existing_type=sa.String(length=20),
                    existing_nullable=False,
                    server_default="PROCESSING",
                )
        else:
            op.alter_column(
                "idempotency_records",
                "state",
                schema=integration_schema,
                server_default="PROCESSING",
            )

    outbox_columns = _columns("integration", "outbox_events")
    if "claimed_by" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column("claimed_by", sa.String(length=80), nullable=True),
            schema=integration_schema,
        )
    if "claimed_at" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            schema=integration_schema,
        )

    constraints = _unique_constraints("b01", "transport_plans")
    if "uq_b01_transport_plans_algorithm_run" not in constraints:
        table_name = "transport_plans" if b01_schema is None else "b01.transport_plans"
        duplicate = op.get_bind().execute(
            sa.text(
                f"SELECT algorithm_run_id FROM {table_name} "
                "GROUP BY algorithm_run_id HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        if duplicate is not None:
            raise RuntimeError("transport_plans 存在重复 algorithm_run_id，无法建立唯一约束")
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("transport_plans") as batch_op:
                batch_op.create_unique_constraint(
                    "uq_b01_transport_plans_algorithm_run",
                    ["algorithm_run_id"],
                )
        else:
            op.create_unique_constraint(
                "uq_b01_transport_plans_algorithm_run",
                "transport_plans",
                ["algorithm_run_id"],
                schema=b01_schema,
            )


def downgrade() -> None:
    bind = op.get_bind()
    integration_schema = None if bind.dialect.name == "sqlite" else "integration"
    b01_schema = None if bind.dialect.name == "sqlite" else "b01"
    constraints = _unique_constraints("b01", "transport_plans")
    if "uq_b01_transport_plans_algorithm_run" in constraints:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("transport_plans") as batch_op:
                batch_op.drop_constraint("uq_b01_transport_plans_algorithm_run", type_="unique")
        else:
            op.drop_constraint(
                "uq_b01_transport_plans_algorithm_run",
                "transport_plans",
                schema=b01_schema,
                type_="unique",
            )
    outbox_columns = _columns("integration", "outbox_events")
    outbox_drop = [name for name in ("claimed_at", "claimed_by") if name in outbox_columns]
    if outbox_drop:
        with op.batch_alter_table("outbox_events", schema=integration_schema) as batch_op:
            for name in outbox_drop:
                batch_op.drop_column(name)
    idempotency_columns = _columns("integration", "idempotency_records")
    idempotency_drop = [name for name in ("completed_at", "state") if name in idempotency_columns]
    if idempotency_drop:
        with op.batch_alter_table("idempotency_records", schema=integration_schema) as batch_op:
            if "state" in idempotency_drop:
                batch_op.drop_index("ix_integration_idempotency_records_state")
            for name in idempotency_drop:
                batch_op.drop_column(name)
