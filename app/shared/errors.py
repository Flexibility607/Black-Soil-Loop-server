from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class BusinessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", "unknown")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "trace_id": trace_id,
        },
    )


def version_conflict(
    current_version: int | None,
    expected_version: int | None = None,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
) -> BusinessError:
    details: dict[str, Any] = {
        "current_version": current_version,
        "expected_version": expected_version,
    }
    if object_type is not None:
        details["object_type"] = object_type
    if object_id is not None:
        details["object_id"] = object_id
    return BusinessError(
        "VERSION_CONFLICT",
        "数据已被其他操作更新，请刷新后重试",
        status_code=409,
        details=details,
    )
