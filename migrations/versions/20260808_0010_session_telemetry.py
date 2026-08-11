"""Add telemetry consistency issues and their B01 projection.

Revision ID: 20260808_0010
Revises: 20260808_0009
Create Date: 2026-08-08
"""

from alembic import op

from app.shared.models import TelemetryIssue, TelemetryIssueProjection

revision = "20260808_0010"
down_revision = "20260808_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    TelemetryIssue.__table__.create(bind=bind, checkfirst=True)
    TelemetryIssueProjection.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    TelemetryIssueProjection.__table__.drop(bind=bind, checkfirst=True)
    TelemetryIssue.__table__.drop(bind=bind, checkfirst=True)
