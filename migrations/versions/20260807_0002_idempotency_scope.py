"""扩展幂等作用域长度以容纳任务和门店复合标识

Revision ID: 20260807_0002
Revises: 20260806_0001
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op

revision = "20260807_0002"
down_revision = "20260806_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "idempotency_records",
        "scope",
        schema="integration",
        existing_type=sa.String(length=80),
        type_=sa.String(length=200),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "idempotency_records",
        "scope",
        schema="integration",
        existing_type=sa.String(length=200),
        type_=sa.String(length=80),
        existing_nullable=False,
    )
