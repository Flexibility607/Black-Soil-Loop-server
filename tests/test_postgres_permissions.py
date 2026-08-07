from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"), reason="需要 PostgreSQL")
def test_b01_b02_database_role_boundaries():
    b01 = create_engine(os.environ["B01_DATABASE_URL"])
    b02 = create_engine(os.environ["B02_DATABASE_URL"])
    with b01.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM b01.transport_orders")) >= 0
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT count(*) FROM b02.transport_tasks"))
    with b02.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM core.stores")) >= 0
        with pytest.raises(DBAPIError):
            connection.execute(text("UPDATE core.stores SET name = name"))
    b01.dispose()
    b02.dispose()
