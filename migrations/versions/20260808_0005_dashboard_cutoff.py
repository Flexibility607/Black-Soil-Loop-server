"""Allow dashboard projections without a business data cutoff.

Revision ID: 20260808_0005
Revises: 20260808_0004
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision = "20260808_0005"
down_revision = "20260808_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    schema = None if bind.dialect.name == "sqlite" else "b01"
    with op.batch_alter_table("dashboard_projections", schema=schema) as batch_op:
        batch_op.alter_column(
            "data_cutoff",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    schema = None if bind.dialect.name == "sqlite" else "b01"
    table_name = "dashboard_projections" if schema is None else "b01.dashboard_projections"
    empty_cutoff = bind.execute(
        sa.text(f"SELECT id FROM {table_name} WHERE data_cutoff IS NULL LIMIT 1")
    ).first()
    if empty_cutoff is not None:
        raise RuntimeError("dashboard_projections 存在空 data_cutoff，无法恢复非空约束")
    with op.batch_alter_table("dashboard_projections", schema=schema) as batch_op:
        batch_op.alter_column(
            "data_cutoff",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=True,
            nullable=False,
        )
