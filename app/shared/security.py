from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.shared.config import get_settings
from app.shared.errors import BusinessError
from app.shared.models import User, UserSession, utcnow

ALGORITHM = "HS256"
PBKDF2_ITERATIONS = 480_000


def as_utc(value: datetime) -> datetime:
    """Normalize SQLite's occasionally-naive timestamps before comparisons."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(computed.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise BusinessError("TOKEN_EXPIRED", "登录已过期", status_code=401) from exc
    except jwt.PyJWTError as exc:
        raise BusinessError("TOKEN_INVALID", "无效的登录凭证", status_code=401) from exc
    if payload.get("type") != expected_type:
        raise BusinessError("TOKEN_TYPE_INVALID", "登录凭证类型错误", status_code=401)
    return payload


def authenticate(db: Session, username: str, password: str) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if not user or not user.active or not verify_password(password, user.password_hash):
        raise BusinessError("INVALID_CREDENTIALS", "用户名或密码错误", status_code=401)
    return user


def issue_tokens(db: Session, user: User) -> IssuedTokens:
    settings = get_settings()
    now = utcnow()
    access_expires = now + timedelta(minutes=settings.access_token_minutes)
    refresh_expires = now + timedelta(days=settings.refresh_token_days)

    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    user.session_version += 1
    session = UserSession(
        user_id=user.id,
        refresh_token_hash="pending",
        session_version=user.session_version,
        expires_at=refresh_expires,
        last_activity_at=now,
    )
    db.add(session)
    db.flush()

    common = {"sub": user.id, "sid": session.id, "ver": user.session_version, "iat": now}
    access_token = _encode({**common, "type": "access", "exp": access_expires})
    refresh_token = _encode({**common, "type": "refresh", "exp": refresh_expires, "nonce": secrets.token_hex(8)})
    session.refresh_token_hash = token_hash(refresh_token)
    db.commit()
    return IssuedTokens(access_token, refresh_token, access_expires, refresh_expires)


def rotate_refresh_token(db: Session, raw_refresh_token: str) -> tuple[User, IssuedTokens]:
    payload = decode_token(raw_refresh_token, "refresh")
    now = utcnow()
    session = db.get(UserSession, payload.get("sid"))
    user = db.get(User, payload.get("sub"))
    if not session or not user or session.revoked_at is not None:
        raise BusinessError("SESSION_INVALID", "会话已失效", status_code=401)
    if as_utc(session.expires_at) <= now or not hmac.compare_digest(
        session.refresh_token_hash, token_hash(raw_refresh_token)
    ):
        raise BusinessError("SESSION_INVALID", "会话已失效", status_code=401)
    if user.session_version != payload.get("ver") or session.session_version != user.session_version:
        raise BusinessError("SESSION_REPLACED", "账号已在其他位置重新登录", status_code=401)
    idle_limit = timedelta(minutes=get_settings().idle_timeout_minutes)
    if now - as_utc(session.last_activity_at) > idle_limit:
        session.revoked_at = now
        db.commit()
        raise BusinessError("SESSION_TIMEOUT", "长时间未操作，已自动退出", status_code=401)
    session.revoked_at = now
    db.flush()
    return user, issue_tokens(db, user)


def revoke_session(db: Session, session_id: str) -> None:
    session = db.get(UserSession, session_id)
    if session and session.revoked_at is None:
        session.revoked_at = utcnow()
        db.commit()
