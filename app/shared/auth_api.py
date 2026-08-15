from __future__ import annotations

import hmac
import secrets

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.shared.config import get_settings
from app.shared.errors import BusinessError
from app.shared.models import DemoCaseInstallation, User
from app.shared.responses import api_payload
from app.shared.security import (
    IssuedTokens,
    authenticate,
    decode_token,
    issue_tokens,
    revoke_session,
    rotate_refresh_token,
)


def dataset_metadata(request: Request, db: Session | None = None) -> dict[str, object | None]:
    dataset_mode = getattr(request.state, "dataset_mode", "live")
    case = getattr(request.state, "demo_case", None)
    if dataset_mode == "showcase" and case is None and db is not None:
        case = db.scalar(
            select(DemoCaseInstallation).where(DemoCaseInstallation.case_key == get_settings().showcase_case_key)
        )
        if case is not None:
            request.state.demo_case = case
    return {
        "dataset_mode": dataset_mode,
        "dataset_label": "固定演示案例" if dataset_mode == "showcase" else None,
        "case_version": case.catalog_version if case is not None else None,
        "case_revision": case.case_revision if case is not None else None,
        "case_refreshed_at": case.refreshed_at.isoformat() if case is not None else None,
    }


def _token_payload(request: Request, user: User, tokens: IssuedTokens, include_refresh: bool, csrf_token: str) -> dict:
    settings = get_settings()
    metadata = dataset_metadata(request)
    return api_payload(
        request,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token if include_refresh else None,
        token_type="bearer",
        access_expires_at=tokens.access_expires_at.isoformat(),
        refresh_expires_at=tokens.refresh_expires_at.isoformat(),
        idle_timeout_seconds=settings.idle_timeout_minutes * 60,
        csrf_token=csrf_token if not include_refresh else None,
        **metadata,
        user={
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "enterprise_id": user.enterprise_id,
            "store_id": user.store_id,
            "driver_id": user.driver_id,
        },
    )


def issue_user_tokens(
    request: Request,
    response: Response,
    db: Session,
    user: User,
    *,
    include_refresh: bool,
    dataset_mode: str = "live",
) -> dict:
    case = None
    if dataset_mode == "showcase":
        case = db.scalar(
            select(DemoCaseInstallation).where(DemoCaseInstallation.case_key == get_settings().showcase_case_key)
        )
        if case is None or case.state != "READY":
            raise BusinessError("SHOWCASE_NOT_READY", "固定演示案例尚未就绪", status_code=503)
        request.state.demo_case = case
    request.state.dataset_mode = dataset_mode
    tokens = issue_tokens(
        db,
        user,
        dataset_mode=dataset_mode,
        case_revision=case.case_revision if case is not None else None,
    )
    settings = get_settings()
    csrf_token = secrets.token_urlsafe(24)
    response.set_cookie(
        "blacksoil_refresh",
        tokens.refresh_token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.refresh_token_days * 86400,
        path="/api/v1",
        domain=settings.cookie_domain,
    )
    response.set_cookie(
        "blacksoil_csrf",
        csrf_token,
        httponly=False,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.refresh_token_days * 86400,
        path="/api/v1",
        domain=settings.cookie_domain,
    )
    return _token_payload(request, user, tokens, include_refresh, csrf_token)


def login_user(
    request: Request,
    response: Response,
    db: Session,
    username: str,
    password: str,
    *,
    include_refresh: bool,
    dataset_mode: str = "live",
) -> dict:
    user = authenticate(db, username, password)
    return issue_user_tokens(
        request,
        response,
        db,
        user,
        include_refresh=include_refresh,
        dataset_mode=dataset_mode,
    )


def refresh_user(
    request: Request,
    response: Response,
    db: Session,
    refresh_token: str | None,
    *,
    include_refresh: bool,
) -> dict:
    raw_token = refresh_token or request.cookies.get("blacksoil_refresh")
    from app.shared.errors import BusinessError

    if not raw_token:
        raise BusinessError("REFRESH_TOKEN_REQUIRED", "缺少刷新凭证", status_code=401)
    if refresh_token is None:
        csrf_cookie = request.cookies.get("blacksoil_csrf") or ""
        csrf_header = request.headers.get("X-CSRF-Token") or ""
        if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_header):
            raise BusinessError("CSRF_VALIDATION_FAILED", "刷新请求缺少有效的 CSRF 凭证", status_code=403)
    token_payload = decode_token(raw_token, "refresh")
    dataset_mode = str(token_payload.get("ds") or "live")
    case = None
    if dataset_mode == "showcase":
        case = db.scalar(
            select(DemoCaseInstallation).where(DemoCaseInstallation.case_key == get_settings().showcase_case_key)
        )
        if case is None or case.state != "READY" or token_payload.get("case_rev") != case.case_revision:
            raise BusinessError("SHOWCASE_CASE_UPDATED", "固定演示案例已更新，请重新登录", status_code=401)
        request.state.demo_case = case
    request.state.dataset_mode = dataset_mode
    user, tokens = rotate_refresh_token(db, raw_token)
    settings = get_settings()
    csrf_token = secrets.token_urlsafe(24)
    response.set_cookie(
        "blacksoil_refresh",
        tokens.refresh_token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.refresh_token_days * 86400,
        path="/api/v1",
        domain=settings.cookie_domain,
    )
    response.set_cookie(
        "blacksoil_csrf",
        csrf_token,
        httponly=False,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.refresh_token_days * 86400,
        path="/api/v1",
        domain=settings.cookie_domain,
    )
    return _token_payload(request, user, tokens, include_refresh, csrf_token)


def logout_user(request: Request, response: Response, db: Session) -> dict:
    revoke_session(db, request.state.session_id)
    response.delete_cookie("blacksoil_refresh", path="/api/v1", domain=get_settings().cookie_domain)
    response.delete_cookie("blacksoil_csrf", path="/api/v1", domain=get_settings().cookie_domain)
    return api_payload(request, success=True)
