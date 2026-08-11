"""Move the legacy dashboard projection to the 30-day cache key.

Revision ID: 20260808_0006
Revises: 20260808_0005
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision = "20260808_0006"
down_revision = "20260808_0005"
branch_labels = None
depends_on = None


def _table_name() -> str:
    return "dashboard_projections" if op.get_bind().dialect.name == "sqlite" else "b01.dashboard_projections"


def upgrade() -> None:
    table_name = _table_name()
    current = op.get_bind().execute(
        sa.text(f"SELECT id FROM {table_name} WHERE id = 'current'")
    ).first()
    target = op.get_bind().execute(
        sa.text(f"SELECT id FROM {table_name} WHERE id = 'current:30d'")
    ).first()
    if current is not None and target is not None:
        raise RuntimeError("dashboard_projections 同时存在 current 和 current:30d，无法安全迁移")
    if current is not None:
        op.execute(sa.text(f"UPDATE {table_name} SET id = 'current:30d' WHERE id = 'current'"))


def downgrade() -> None:
    table_name = _table_name()
    current = op.get_bind().execute(
        sa.text(f"SELECT id FROM {table_name} WHERE id = 'current'")
    ).first()
    target = op.get_bind().execute(
        sa.text(f"SELECT id FROM {table_name} WHERE id = 'current:30d'")
    ).first()
    if current is not None and target is not None:
        raise RuntimeError("dashboard_projections 同时存在 current 和 current:30d，无法安全回退")
    if target is not None:
        op.execute(sa.text(f"UPDATE {table_name} SET id = 'current' WHERE id = 'current:30d'"))
