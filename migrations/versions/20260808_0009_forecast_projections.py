"""Add next-week demand forecast projections and batches.

Revision ID: 20260808_0009
Revises: 20260808_0008
Create Date: 2026-08-08
"""

from alembic import op

from app.shared.models import DemandForecastBatch, DemandForecastProjection

revision = "20260808_0009"
down_revision = "20260808_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    DemandForecastProjection.__table__.create(bind=bind, checkfirst=True)
    DemandForecastBatch.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    DemandForecastBatch.__table__.drop(bind=bind, checkfirst=True)
    DemandForecastProjection.__table__.drop(bind=bind, checkfirst=True)
