from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"), reason="需要 PostgreSQL")
def test_b01_b02_database_role_boundaries():
    b01 = create_engine(os.environ["B01_DATABASE_URL"])
    b02 = create_engine(os.environ["B02_DATABASE_URL"])
    worker = create_engine(os.environ["WORKER_DATABASE_URL"])
    with b01.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM b01.transport_orders")) >= 0
        assert connection.scalar(text("SELECT count(*) FROM b01.voice_quota_buckets")) >= 0
        assert connection.scalar(text("SELECT count(*) FROM b01.dashboard_map_points")) >= 0
        connection.commit()
        with pytest.raises(DBAPIError):
            connection.execute(
                text("UPDATE b01.dashboard_map_points SET display_name = display_name")
            )
        connection.rollback()
        transaction = connection.begin()
        quota_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO b01.voice_quota_buckets "
                "(id, subject_hash, scope, window_started_at, window_seconds, request_count, updated_at) "
                "VALUES (:id, repeat('b', 64), 'permission-test', now(), 60, 1, now())"
            ),
            {"id": quota_id},
        )
        connection.execute(
            text("UPDATE b01.voice_quota_buckets SET request_count = 2 WHERE id = :id"),
            {"id": quota_id},
        )
        connection.execute(text("DELETE FROM b01.voice_quota_buckets WHERE id = :id"), {"id": quota_id})
        transaction.rollback()
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT count(*) FROM b02.transport_tasks"))
    with b02.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM core.stores")) >= 0
        with pytest.raises(DBAPIError):
            connection.execute(text("UPDATE core.stores SET name = name"))
        connection.rollback()
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO b01.voice_quota_buckets "
                    "(id, subject_hash, scope, window_started_at, window_seconds, request_count, updated_at) "
                    "VALUES ('permission-test', repeat('a', 64), 'test', now(), 60, 1, now())"
                )
            )
        connection.rollback()
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT transcript FROM b01.voice_transcription_requests"))
        connection.rollback()
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT display_name FROM b01.dashboard_map_points"))
    with worker.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM b01.voice_transcription_requests")) >= 0
        assert connection.scalar(text("SELECT count(*) FROM b01.dashboard_map_points")) >= 0
        assert connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'b01.dashboard_map_points', 'INSERT,UPDATE,DELETE')"
            )
        )
        connection.commit()
        transaction = connection.begin()
        request_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO b01.voice_transcription_requests "
                "(id, request_scope, subject_hash, idempotency_key, client_request_id, request_hash, "
                "state, status_code, duration_seconds, created_at) "
                "VALUES (:id, 'PUBLIC', repeat('c', 64), :key, :client, repeat('d', 64), "
                "'PROCESSING', 0, 1, now())"
            ),
            {"id": request_id, "key": str(uuid4()), "client": str(uuid4())},
        )
        connection.execute(
            text(
                "UPDATE b01.voice_transcription_requests "
                "SET state = 'FAILED', status_code = 409 WHERE id = :id"
            ),
            {"id": request_id},
        )
        connection.execute(
            text("DELETE FROM b01.voice_transcription_requests WHERE id = :id"),
            {"id": request_id},
        )
        transaction.rollback()
    b01.dispose()
    b02.dispose()
    worker.dispose()


@pytest.mark.skipif(
    not os.environ.get("SHOWCASE_B01_DATABASE_URL", "").startswith("postgresql"),
    reason="需要独立 PostgreSQL 演示数据库和角色",
)
def test_showcase_database_roles_are_isolated_from_live_database():
    showcase_b01_url = make_url(os.environ["SHOWCASE_B01_DATABASE_URL"])
    showcase_b02_url = make_url(os.environ["SHOWCASE_B02_DATABASE_URL"])
    showcase_worker_url = make_url(os.environ["SHOWCASE_WORKER_DATABASE_URL"])
    showcase_b01 = create_engine(showcase_b01_url)
    showcase_b02 = create_engine(showcase_b02_url)
    showcase_worker = create_engine(showcase_worker_url)

    with showcase_b01.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "black_soil_loop_showcase"
        assert connection.scalar(text("SELECT count(*) FROM b01.transport_orders")) >= 0
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT count(*) FROM b02.transport_tasks"))
        connection.rollback()
        with pytest.raises(DBAPIError):
            connection.execute(text("UPDATE b01.dashboard_map_points SET display_name = display_name"))
        connection.rollback()

    with showcase_b02.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "black_soil_loop_showcase"
        assert connection.scalar(text("SELECT count(*) FROM core.stores")) >= 0
        assert connection.scalar(text("SELECT count(*) FROM b02.transport_tasks")) >= 0
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT count(*) FROM b01.algorithm_runs"))
        connection.rollback()

    with showcase_worker.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "black_soil_loop_showcase"
        assert connection.scalar(text("SELECT count(*) FROM integration.demo_case_installations")) >= 0
        assert connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'integration.demo_case_installations', 'INSERT,UPDATE,DELETE')"
            )
        )

    for url in (showcase_b01_url, showcase_b02_url, showcase_worker_url):
        forbidden_live = create_engine(url.set(database="black_soil_loop"))
        with pytest.raises(DBAPIError), forbidden_live.connect():
            pass
        forbidden_live.dispose()

    live_b01_url = make_url(os.environ["B01_DATABASE_URL"])
    forbidden_showcase = create_engine(live_b01_url.set(database="black_soil_loop_showcase"))
    with pytest.raises(DBAPIError), forbidden_showcase.connect():
        pass
    forbidden_showcase.dispose()
    showcase_b01.dispose()
    showcase_b02.dispose()
    showcase_worker.dispose()
