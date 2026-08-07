from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Request

from app.shared.models import utcnow

SCHEMA_VERSION = "1.0"


def api_payload(request: Request, *, data_cutoff: datetime | str | None = None, **values: Any) -> dict[str, Any]:
    payload = {
        **values,
        "trace_id": request.state.trace_id,
        "schema_version": SCHEMA_VERSION,
        "generated_at": utcnow().isoformat(),
    }
    if data_cutoff is not None:
        payload["data_cutoff"] = data_cutoff.isoformat() if isinstance(data_cutoff, datetime) else str(data_cutoff)
    return payload


def page_payload(
    request: Request,
    items: list[Any],
    *,
    page: int,
    page_size: int,
    total: int,
) -> dict[str, Any]:
    return api_payload(request, items=items, page=page, page_size=page_size, total=total)
