"""Add Changchun map catalog and algorithm showcase metadata.

Revision ID: 20260813_0013
Revises: 20260811_0012
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op

revision = "20260813_0013"
down_revision = "20260811_0012"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return None if op.get_bind().dialect.name == "sqlite" else "b01"


def upgrade() -> None:
    bind = op.get_bind()
    schema = _schema()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names(schema=schema))
    if "dashboard_map_points" not in table_names:
        op.create_table(
            "dashboard_map_points",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("point_code", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=36), nullable=True),
            sa.Column("display_name", sa.String(length=120), nullable=False),
            sa.Column("point_type", sa.String(length=30), nullable=False),
            sa.Column("brand_name", sa.String(length=80), nullable=False),
            sa.Column("address", sa.String(length=240), nullable=False),
            sa.Column("longitude", sa.Numeric(10, 6, asdecimal=False), nullable=False),
            sa.Column("latitude", sa.Numeric(10, 6, asdecimal=False), nullable=False),
            sa.Column("coordinate_crs", sa.String(length=20), nullable=False, server_default="EPSG:4326"),
            sa.Column("coordinate_accuracy", sa.String(length=30), nullable=False),
            sa.Column("source_longitude", sa.Numeric(10, 6, asdecimal=False), nullable=True),
            sa.Column("source_latitude", sa.Numeric(10, 6, asdecimal=False), nullable=True),
            sa.Column("source_crs", sa.String(length=20), nullable=False),
            sa.Column("conversion_method", sa.String(length=120), nullable=True),
            sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("display_order", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(length=30), nullable=False),
            sa.Column("source_name", sa.String(length=120), nullable=False),
            sa.Column("source_url", sa.String(length=500), nullable=True),
            sa.Column("source_accessed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("catalog_version", sa.String(length=40), nullable=False),
            sa.Column("catalog_sha256", sa.String(length=64), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("object_version", sa.Integer(), nullable=False, server_default="1"),
            sa.CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_b01_map_point_longitude"),
            sa.CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_b01_map_point_latitude"),
            sa.ForeignKeyConstraint(["store_id"], ["core.stores.id"] if schema else ["stores.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("point_code", name="uq_b01_dashboard_map_point_code"),
            schema=schema,
        )
        op.create_index(
            "ix_b01_dashboard_map_point_order",
            "dashboard_map_points",
            ["enabled", "featured", "display_order"],
            schema=schema,
        )
        op.create_index(
            "ix_b01_dashboard_map_points_catalog_version",
            "dashboard_map_points",
            ["catalog_version"],
            schema=schema,
        )
        op.create_index(
            "ix_b01_dashboard_map_points_point_type",
            "dashboard_map_points",
            ["point_type"],
            schema=schema,
        )
        op.create_index(
            "ix_b01_dashboard_map_points_store_id",
            "dashboard_map_points",
            ["store_id"],
            schema=schema,
        )
    else:
        required = {
            "point_code",
            "display_name",
            "point_type",
            "longitude",
            "latitude",
            "catalog_version",
            "object_version",
        }
        columns = {item["name"] for item in inspector.get_columns("dashboard_map_points", schema=schema)}
        if not required.issubset(columns):
            missing = ", ".join(sorted(required - columns))
            raise RuntimeError(f"dashboard_map_points 结构不完整，缺少列: {missing}")

    inspector = sa.inspect(bind)
    algorithm_columns = {item["name"] for item in inspector.get_columns("algorithm_runs", schema=schema)}
    new_columns = {
        "showcase_key": sa.Column("showcase_key", sa.String(length=48), nullable=True),
        "showcase_batch_key": sa.Column("showcase_batch_key", sa.String(length=64), nullable=True),
        "input_signature": sa.Column("input_signature", sa.String(length=64), nullable=True),
        "input_data_cutoff": sa.Column("input_data_cutoff", sa.DateTime(timezone=True), nullable=True),
        "showcase_enabled": sa.Column("showcase_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        "showcase_source_mode": sa.Column("showcase_source_mode", sa.String(length=30), nullable=True),
    }
    for name, column in new_columns.items():
        if name not in algorithm_columns:
            op.add_column("algorithm_runs", column, schema=schema)

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("algorithm_runs", schema=schema)}
    if "ix_b01_algorithm_runs_showcase" not in indexes:
        op.create_index(
            "ix_b01_algorithm_runs_showcase",
            "algorithm_runs",
            ["showcase_enabled", "algorithm_type", "showcase_key", "created_at"],
            schema=schema,
        )
    signature_name = "uq_b01_algorithm_showcase_signature"
    unique_names = {item["name"] for item in sa.inspect(bind).get_unique_constraints("algorithm_runs", schema=schema)}
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("algorithm_runs", schema=schema)}
    if signature_name not in unique_names and signature_name not in indexes:
        columns = ["algorithm_type", "showcase_key", "showcase_batch_key", "input_signature"]
        if bind.dialect.name == "sqlite":
            # SQLite cannot add a table constraint after CREATE TABLE. A unique
            # index has the same NULL-aware uniqueness semantics required here.
            op.create_index(signature_name, "algorithm_runs", columns, unique=True, schema=schema)
        else:
            op.create_unique_constraint(signature_name, "algorithm_runs", columns, schema=schema)


def downgrade() -> None:
    schema = _schema()
    if op.get_bind().dialect.name == "sqlite":
        op.drop_index("uq_b01_algorithm_showcase_signature", table_name="algorithm_runs", schema=schema)
    else:
        op.drop_constraint(
            "uq_b01_algorithm_showcase_signature",
            "algorithm_runs",
            schema=schema,
            type_="unique",
        )
    op.drop_index("ix_b01_algorithm_runs_showcase", table_name="algorithm_runs", schema=schema)
    for column_name in (
        "showcase_source_mode",
        "showcase_enabled",
        "input_data_cutoff",
        "input_signature",
        "showcase_batch_key",
        "showcase_key",
    ):
        op.drop_column("algorithm_runs", column_name, schema=schema)
    op.drop_table("dashboard_map_points", schema=schema)
