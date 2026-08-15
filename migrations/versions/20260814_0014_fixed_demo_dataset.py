"""Add fixed showcase dataset installation metadata.

Revision ID: 20260814_0014
Revises: 20260813_0013
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "20260814_0014"
down_revision = "20260813_0013"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return None if op.get_bind().dialect.name == "sqlite" else "integration"


def upgrade() -> None:
    bind = op.get_bind()
    schema = _schema()
    inspector = sa.inspect(bind)
    if "demo_case_installations" in set(inspector.get_table_names(schema=schema)):
        required = {
            "case_key",
            "catalog_version",
            "catalog_sha256",
            "anchor_date",
            "state",
            "case_revision",
            "object_version",
        }
        columns = {item["name"] for item in inspector.get_columns("demo_case_installations", schema=schema)}
        if not required.issubset(columns):
            missing = ", ".join(sorted(required - columns))
            raise RuntimeError(f"demo_case_installations is missing required columns: {missing}")
        return

    op.create_table(
        "demo_case_installations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_key", sa.String(length=80), nullable=False),
        sa.Column("catalog_version", sa.String(length=40), nullable=False),
        sa.Column("catalog_sha256", sa.String(length=64), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="READY"),
        sa.Column("case_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reset_reason", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('READY', 'REFRESHING', 'FAILED')",
            name="ck_integration_demo_case_state",
        ),
        sa.CheckConstraint(
            "case_revision >= 1",
            name="ck_integration_demo_case_revision_positive",
        ),
        sa.UniqueConstraint("case_key", name="uq_integration_demo_case_key"),
        schema=schema,
    )
    op.create_index(
        "ix_integration_demo_case_installations_state",
        "demo_case_installations",
        ["state"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index(
        "ix_integration_demo_case_installations_state",
        table_name="demo_case_installations",
        schema=schema,
    )
    op.drop_table("demo_case_installations", schema=schema)
