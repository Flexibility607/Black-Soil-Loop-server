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
    bind = op.get_bind()
    schema = None if bind.dialect.name == "sqlite" else "integration"
    column = next(
        item
        for item in sa.inspect(bind).get_columns("idempotency_records", schema=schema)
        if item["name"] == "scope"
    )
    if getattr(column["type"], "length", None) == 200:
        return
    with op.batch_alter_table("idempotency_records", schema=schema) as batch_op:
        batch_op.alter_column(
            "scope",
            existing_type=sa.String(length=80),
            type_=sa.String(length=200),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    schema = None if bind.dialect.name == "sqlite" else "integration"
    column = next(
        item
        for item in sa.inspect(bind).get_columns("idempotency_records", schema=schema)
        if item["name"] == "scope"
    )
    if getattr(column["type"], "length", None) == 80:
        return
    with op.batch_alter_table("idempotency_records", schema=schema) as batch_op:
        batch_op.alter_column(
            "scope",
            existing_type=sa.String(length=200),
            type_=sa.String(length=80),
            existing_nullable=False,
        )
