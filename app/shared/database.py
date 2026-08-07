from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.shared.config import get_settings

SCHEMAS = ("iam", "core", "b01", "b02", "integration")


class Base(DeclarativeBase):
    pass


def _create_engine() -> Engine:
    settings = get_settings()
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    database_url = settings.effective_database_url
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["execution_options"] = {"schema_translate_map": {schema: None for schema in SCHEMAS}}
    return create_engine(database_url, **kwargs)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_schemas(target_engine: Engine = engine) -> None:
    if target_engine.dialect.name != "postgresql":
        return
    with target_engine.begin() as connection:
        for schema in SCHEMAS:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def init_database(target_engine: Engine = engine) -> None:
    from app.shared import models  # noqa: F401

    create_schemas(target_engine)
    Base.metadata.create_all(target_engine)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
