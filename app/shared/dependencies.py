from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.shared.config import get_settings
from app.shared.database import get_db
from app.shared.errors import BusinessError
from app.shared.models import DemoCaseInstallation, User, UserSession, utcnow
from app.shared.security import as_utc, decode_token

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise BusinessError("AUTH_REQUIRED", "请先登录", status_code=401)
    payload = decode_token(credentials.credentials, "access")
    dataset_mode = str(payload.get("ds") or "live")
    if dataset_mode != getattr(request.state, "dataset_mode", "live"):
        raise BusinessError("DATASET_TOKEN_MISMATCH", "登录数据集无效", status_code=401)
    if dataset_mode == "showcase":
        case = db.scalar(
            select(DemoCaseInstallation).where(DemoCaseInstallation.case_key == get_settings().showcase_case_key)
        )
        if case is None or case.state != "READY" or payload.get("case_rev") != case.case_revision:
            raise BusinessError("SHOWCASE_CASE_UPDATED", "固定演示案例已更新，请重新登录", status_code=401)
        request.state.demo_case = case
    user = db.get(User, payload.get("sub"))
    session = db.get(UserSession, payload.get("sid"))
    if not user or not user.active or not session or session.revoked_at is not None:
        raise BusinessError("SESSION_INVALID", "会话已失效", status_code=401)
    if payload.get("ver") != user.session_version or session.session_version != user.session_version:
        raise BusinessError("SESSION_REPLACED", "账号已在其他位置重新登录", status_code=401)
    now = utcnow()
    if now - as_utc(session.last_activity_at) > timedelta(minutes=get_settings().idle_timeout_minutes):
        session.revoked_at = now
        db.commit()
        raise BusinessError("SESSION_TIMEOUT", "长时间未操作，已自动退出", status_code=401)
    session.last_activity_at = now
    db.commit()
    request.state.session_id = session.id
    request.state.current_user = user
    return user


def require_roles(*allowed_roles: str) -> Callable[[User], User]:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise BusinessError("FORBIDDEN", "当前账号无权执行此操作", status_code=403)
        return user

    return dependency
