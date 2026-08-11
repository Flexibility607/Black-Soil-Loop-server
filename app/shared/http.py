from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm.exc import StaleDataError
from starlette.formparsers import MultiPartParser

from app.shared.config import get_settings
from app.shared.errors import BusinessError, business_error_handler, version_conflict
from app.shared.rate_limit import FixedWindowRateLimiter


def configure_app(app: FastAPI) -> FastAPI:
    settings = get_settings()
    voice_request_max_bytes = settings.voice_max_bytes + 128 * 1024
    # Voice WAV files accepted by the application stay in memory. Oversized
    # requests are still rejected by the application and the reverse proxy.
    MultiPartParser.spool_max_size = max(MultiPartParser.spool_max_size, voice_request_max_bytes)
    limiter = FixedWindowRateLimiter()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Trace-Id",
            "X-CSRF-Token",
            "X-Device-Key",
        ],
    )
    app.add_exception_handler(BusinessError, business_error_handler)

    @app.exception_handler(StaleDataError)
    async def stale_data_handler(request: Request, _: StaleDataError) -> JSONResponse:
        return await business_error_handler(request, version_conflict(None, None))

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        supplied_trace = request.headers.get("X-Trace-Id")
        try:
            request.state.trace_id = str(UUID(supplied_trace)) if supplied_trace else str(uuid4())
        except (TypeError, ValueError):
            request.state.trace_id = str(uuid4())
        path = request.url.path
        transcription_paths = {
            "/api/v1/public/assistant/transcriptions",
            "/api/v1/web/assistant/transcriptions",
        }
        if path in transcription_paths:
            raw_length = request.headers.get("Content-Length")
            try:
                content_length = int(raw_length) if raw_length is not None else None
            except ValueError:
                content_length = -1
            if content_length is None or content_length < 0:
                return JSONResponse(
                    status_code=411,
                    headers={"X-Trace-Id": request.state.trace_id},
                    content={
                        "code": "CONTENT_LENGTH_REQUIRED",
                        "message": "语音上传必须提供有效的 Content-Length",
                        "details": {},
                        "trace_id": request.state.trace_id,
                    },
                )
            if content_length > voice_request_max_bytes:
                return JSONResponse(
                    status_code=413,
                    headers={"X-Trace-Id": request.state.trace_id},
                    content={
                        "code": "AUDIO_TOO_LARGE",
                        "message": "语音上传请求超过允许大小",
                        "details": {"maximum_request_bytes": voice_request_max_bytes},
                        "trace_id": request.state.trace_id,
                    },
                )
        if path.startswith("/api/") and not path.startswith("/api/v1/dashboard/events"):
            # Uvicorn accepts proxy headers only from the loopback Nginx peer.
            # Never consume forwarding headers here because clients can forge them.
            client = request.client.host if request.client else "unknown"
            scope, limit = (
                ("device", settings.device_rate_limit_per_minute)
                if path.startswith("/api/v1/device/")
                else ("auth", settings.auth_rate_limit_per_minute)
                if "/auth/" in path
                else ("api", settings.rate_limit_per_minute)
            )
            allowed, retry_after = await limiter.allow(f"{client}:{scope}", limit)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after), "X-Trace-Id": request.state.trace_id},
                    content={
                        "code": "RATE_LIMITED",
                        "message": "请求过于频繁，请稍后重试",
                        "details": {"retry_after_seconds": retry_after},
                        "trace_id": request.state.trace_id,
                    },
                )
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求数据校验失败",
                "details": {"errors": jsonable_encoder(exc.errors())},
                "trace_id": request.state.trace_id,
            },
        )

    return app
