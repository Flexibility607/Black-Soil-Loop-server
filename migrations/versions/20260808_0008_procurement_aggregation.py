"""Add confirmed demand and procurement aggregation snapshots.

Revision ID: 20260808_0008
Revises: 20260808_0007
Create Date: 2026-08-08
"""

from alembic import op

from app.shared.models import ProcurementAggregation, ProcurementDemandConfirmation

revision = "20260808_0008"
down_revision = "20260808_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ProcurementDemandConfirmation.__table__.create(bind=bind, checkfirst=True)
    ProcurementAggregation.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    ProcurementAggregation.__table__.drop(bind=bind, checkfirst=True)
    ProcurementDemandConfirmation.__table__.drop(bind=bind, checkfirst=True)
