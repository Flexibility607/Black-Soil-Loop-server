from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from fastapi import Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.shared.config import get_settings

SCHEMAS = ("iam", "core", "b01", "b02", "integration")


class Base(DeclarativeBase):
    pass


def _create_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    database_url = database_url or (
        settings.effective_showcase_database_url
        if settings.dataset_role == "showcase"
        else settings.effective_database_url
    )
    if not database_url:
        raise RuntimeError("showcase database URL is not configured")
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["execution_options"] = {"schema_translate_map": {schema: None for schema in SCHEMAS}}
    return create_engine(database_url, **kwargs)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

_showcase_url = get_settings().effective_showcase_database_url
showcase_engine = _create_engine(_showcase_url) if _showcase_url else None
ShowcaseSessionLocal = (
    sessionmaker(bind=showcase_engine, autoflush=False, expire_on_commit=False) if showcase_engine is not None else None
)


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


def _request_dataset(request: Request) -> str:
    settings = get_settings()
    path = request.url.path
    if path.startswith("/api/v1/public/") or path == "/api/v1/dashboard/events":
        return settings.public_dashboard_dataset
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        from app.shared.security import decode_token

        payload = decode_token(authorization.split(" ", 1)[1], "access")
        return str(payload.get("ds") or "live")
    return "live"


def sessionmaker_for_dataset(dataset_mode: str):
    normalized = dataset_mode.strip().lower()
    if normalized == "live":
        return SessionLocal
    if normalized != "showcase":
        from app.shared.errors import BusinessError

        raise BusinessError("DATASET_INVALID", "数据集无效", status_code=401)
    settings = get_settings()
    if not settings.showcase_dataset_enabled or ShowcaseSessionLocal is None:
        from app.shared.errors import BusinessError

        raise BusinessError("SHOWCASE_UNAVAILABLE", "固定演示案例暂不可用", status_code=503)
    return ShowcaseSessionLocal


@contextmanager
def session_for_dataset(dataset_mode: str):
    maker = sessionmaker_for_dataset(dataset_mode)
    with maker() as session:
        yield session


def get_db(request: Request) -> Generator[Session, None, None]:
    dataset_mode = _request_dataset(request)
    request.state.dataset_mode = dataset_mode
    maker = sessionmaker_for_dataset(dataset_mode)
    with maker() as session:
        yield session
