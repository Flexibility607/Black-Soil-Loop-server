"""Add B01 anonymous assistant quota, lease, and replay tables.

Revision ID: 20260811_0012
Revises: 20260808_0011
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "20260811_0012"
down_revision = "20260808_0011"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return None if op.get_bind().dialect.name == "sqlite" else "b01"


def upgrade() -> None:
    schema = _schema()
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names(schema=schema))
    voice_tables = {
        "voice_quota_buckets",
        "voice_concurrency_leases",
        "voice_transcription_requests",
    }
    present = table_names & voice_tables
    if present:
        # The historical 0001 migration builds current metadata on a new empty
        # database. Such a fresh upgrade already has all three current tables;
        # a partial pre-existing set is ambiguous and must fail explicitly.
        if present != voice_tables:
            missing = ", ".join(sorted(voice_tables - present))
            raise RuntimeError(f"语音助手表只存在部分结构，缺少: {missing}")
        required_columns = {
            "voice_quota_buckets": {"subject_hash", "scope", "window_started_at", "request_count"},
            "voice_concurrency_leases": {"subject_hash", "client_request_id", "expires_at"},
            "voice_transcription_requests": {
                "request_scope",
                "subject_hash",
                "idempotency_key",
                "request_hash",
                "state",
                "transcript",
                "replay_expires_at",
            },
        }
        inspector = sa.inspect(bind)
        for table_name, required in required_columns.items():
            columns = {item["name"] for item in inspector.get_columns(table_name, schema=schema)}
            if not required.issubset(columns):
                missing = ", ".join(sorted(required - columns))
                raise RuntimeError(f"{table_name} 结构不完整，缺少列: {missing}")
        return
    op.create_table(
        "voice_quota_buckets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("request_count >= 0", name="ck_b01_voice_quota_count_nonnegative"),
        sa.CheckConstraint("window_seconds > 0", name="ck_b01_voice_quota_window_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_hash",
            "scope",
            "window_started_at",
            name="uq_b01_voice_quota_window",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_b01_voice_quota_scope_window",
        "voice_quota_buckets",
        ["scope", "window_started_at"],
        schema=schema,
    )
    op.create_table(
        "voice_concurrency_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_hash",
            "client_request_id",
            name="uq_b01_voice_lease_subject_request",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_b01_voice_concurrency_leases_subject_hash",
        "voice_concurrency_leases",
        ["subject_hash"],
        schema=schema,
    )
    op.create_index(
        "ix_b01_voice_lease_expires",
        "voice_concurrency_leases",
        ["expires_at"],
        schema=schema,
    )
    op.create_table(
        "voice_transcription_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_scope", sa.String(length=20), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="PROCESSING"),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("transcript_sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("duration_seconds > 0", name="ck_b01_voice_transcription_duration_positive"),
        sa.CheckConstraint(
            "state IN ('PROCESSING', 'COMPLETED', 'FAILED')",
            name="ck_b01_voice_transcription_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_scope",
            "subject_hash",
            "idempotency_key",
            name="uq_b01_voice_transcription_subject_key",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_b01_voice_transcription_state_expiry",
        "voice_transcription_requests",
        ["state", "replay_expires_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("voice_transcription_requests", schema=schema)
    op.drop_table("voice_concurrency_leases", schema=schema)
    op.drop_table("voice_quota_buckets", schema=schema)
