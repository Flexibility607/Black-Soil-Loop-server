from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.shared.errors import BusinessError, version_conflict
from app.shared.models import utcnow


def atomic_versioned_update[VersionedModel](
    db: Session,
    model: type[VersionedModel],
    object_id: str,
    expected_version: int,
    values: Mapping[str, Any],
    *,
    conditions: Iterable[ColumnElement[bool]] = (),
) -> int:
    """Atomically update one versioned row without depending on ORM state."""

    object_type = getattr(model, "__tablename__", model.__name__)
    protected = {"id", "object_version", "created_at", "updated_at"}
    invalid = protected.intersection(values)
    if invalid:
        fields = ", ".join(sorted(invalid))
        raise ValueError(f"原子版本更新不能直接设置字段：{fields}")

    statement = (
        update(model)
        .where(
            model.id == object_id,
            model.object_version == expected_version,
            *tuple(conditions),
        )
        .values(
            **dict(values),
            object_version=model.object_version + 1,
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    result = db.execute(statement)
    if result.rowcount == 1:
        return expected_version + 1

    database_version = db.scalar(select(model.object_version).where(model.id == object_id))
    if database_version != expected_version:
        raise version_conflict(
            database_version,
            expected_version,
            object_type=object_type,
            object_id=object_id,
        )
    raise BusinessError(
        "STATE_GUARD_CONFLICT",
        "对象状态已变化，请刷新后重试",
        status_code=409,
        details={
            "object_type": object_type,
            "object_id": object_id,
            "object_version": expected_version,
        },
    )
