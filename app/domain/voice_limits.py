from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from math import ceil

from fastapi import Request
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.shared.config import get_settings
from app.shared.errors import BusinessError
from app.shared.models import VoiceConcurrencyLease, VoiceQuotaBucket, new_id, utcnow
from app.shared.periods import SHANGHAI

GLOBAL_SUBJECT = hashlib.sha256(b"black-soil-loop:voice:global").hexdigest()
CONCURRENCY_LOCK_ID = 7_620_260_811


@dataclass(frozen=True)
class VoiceQuotaClaim:
    lease_id: str | None
    daily_warning: bool = False


def canonical_client_ip(request: Request) -> str:
    value = request.client.host if request.client else "unknown"
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return value.strip().lower() or "unknown"


def anonymous_subject_hash(request: Request) -> str:
    secret = get_settings().voice_rate_limit_hmac_secret
    secret_value = secret.get_secret_value().strip() if secret is not None else ""
    if len(secret_value) < 32:
        raise BusinessError(
            "TRANSCRIPTION_NOT_CONFIGURED",
            "语音助手匿名限流密钥尚未配置",
            status_code=503,
        )
    return hmac.new(
        secret_value.encode("utf-8"),
        canonical_client_ip(request).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def authenticated_subject_hash(user_id: str) -> str:
    secret = get_settings().voice_rate_limit_hmac_secret
    secret_value = secret.get_secret_value().strip() if secret is not None else ""
    if len(secret_value) < 32:
        raise BusinessError(
            "TRANSCRIPTION_NOT_CONFIGURED",
            "语音助手限流密钥尚未配置",
            status_code=503,
        )
    return hmac.new(
        secret_value.encode("utf-8"),
        f"user:{user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _fixed_window(now: datetime, seconds: int) -> datetime:
    timestamp = int(now.timestamp())
    return datetime.fromtimestamp(timestamp - timestamp % seconds, UTC)


def _shanghai_day_window(now: datetime) -> datetime:
    local_day = now.astimezone(SHANGHAI).date()
    return datetime.combine(local_day, time.min, tzinfo=SHANGHAI).astimezone(UTC)


def _increment_bucket(
    db: Session,
    *,
    subject_hash: str,
    scope: str,
    window_started_at: datetime,
    window_seconds: int,
    limit: int,
    now: datetime,
) -> int | None:
    table = VoiceQuotaBucket.__table__
    values = {
        "id": new_id(),
        "subject_hash": subject_hash,
        "scope": scope,
        "window_started_at": window_started_at,
        "window_seconds": window_seconds,
        "request_count": 1,
        "updated_at": now,
    }
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        statement = postgresql_insert(table).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(table).values(**values)
    else:
        raise RuntimeError(f"语音助手配额不支持数据库方言 {dialect}")
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.subject_hash, table.c.scope, table.c.window_started_at],
        set_={"request_count": table.c.request_count + 1, "updated_at": now},
        where=table.c.request_count < limit,
    ).returning(table.c.request_count)
    return db.execute(statement).scalar_one_or_none()


def _consume(
    db: Session,
    *,
    subject_hash: str,
    scope: str,
    start: datetime,
    seconds: int,
    limit: int,
    now: datetime,
    error_code: str,
) -> int:
    count = _increment_bucket(
        db,
        subject_hash=subject_hash,
        scope=scope,
        window_started_at=start,
        window_seconds=seconds,
        limit=limit,
        now=now,
    )
    if count is None:
        retry_after = max(
            1,
            ceil((start + timedelta(seconds=seconds) - now).total_seconds()),
        )
        raise BusinessError(
            error_code,
            "语音或问答请求已达到当前额度，请稍后重试",
            status_code=429,
            details={"retry_after_seconds": retry_after},
        )
    return count


def _cleanup_expired_buckets(db: Session, now: datetime) -> None:
    # Keep one extra day for aggregate monitoring, then remove expired counters.
    db.execute(delete(VoiceQuotaBucket).where(VoiceQuotaBucket.window_started_at < now - timedelta(days=2)))


def claim_public_text_quota(db: Session, subject_hash: str, now: datetime | None = None) -> None:
    settings = get_settings()
    current = now or utcnow()
    try:
        _consume(
            db,
            subject_hash=subject_hash,
            scope="public-text-minute",
            start=_fixed_window(current, 60),
            seconds=60,
            limit=settings.voice_public_text_per_minute,
            now=current,
            error_code="VOICE_RATE_LIMITED",
        )
        _consume(
            db,
            subject_hash=subject_hash,
            scope="public-text-day",
            start=_shanghai_day_window(current),
            seconds=86_400,
            limit=settings.voice_public_text_per_day,
            now=current,
            error_code="VOICE_RATE_LIMITED",
        )
        _cleanup_expired_buckets(db, current)
        db.commit()
    except Exception:
        db.rollback()
        raise


def claim_voice_quota(
    db: Session,
    subject_hash: str,
    client_request_id: str,
    now: datetime | None = None,
) -> VoiceQuotaClaim:
    settings = get_settings()
    current = now or utcnow()
    try:
        _consume(
            db,
            subject_hash=subject_hash,
            scope="public-voice-minute",
            start=_fixed_window(current, 60),
            seconds=60,
            limit=settings.voice_public_per_minute,
            now=current,
            error_code="VOICE_RATE_LIMITED",
        )
        _consume(
            db,
            subject_hash=subject_hash,
            scope="public-voice-hour",
            start=_fixed_window(current, 3_600),
            seconds=3_600,
            limit=settings.voice_public_per_hour,
            now=current,
            error_code="VOICE_RATE_LIMITED",
        )
        _consume(
            db,
            subject_hash=subject_hash,
            scope="public-voice-day",
            start=_shanghai_day_window(current),
            seconds=86_400,
            limit=settings.voice_public_per_day,
            now=current,
            error_code="VOICE_RATE_LIMITED",
        )
        global_count = _consume(
            db,
            subject_hash=GLOBAL_SUBJECT,
            scope="global-voice-day",
            start=_shanghai_day_window(current),
            seconds=86_400,
            limit=settings.voice_global_per_day,
            now=current,
            error_code="VOICE_DAILY_BUDGET_EXHAUSTED",
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": CONCURRENCY_LOCK_ID})
        db.execute(delete(VoiceConcurrencyLease).where(VoiceConcurrencyLease.expires_at <= current))
        active = db.scalar(select(func.count()).select_from(VoiceConcurrencyLease)) or 0
        if active >= settings.voice_max_concurrency:
            raise BusinessError(
                "VOICE_CONCURRENCY_EXCEEDED",
                "当前语音识别请求较多，请稍后重试",
                status_code=429,
                details={"retry_after_seconds": settings.voice_lease_seconds},
            )
        lease_id = new_id()
        db.add(
            VoiceConcurrencyLease(
                id=lease_id,
                subject_hash=subject_hash,
                client_request_id=client_request_id,
                expires_at=current + timedelta(seconds=settings.voice_lease_seconds),
            )
        )
        _cleanup_expired_buckets(db, current)
        db.flush()
        db.commit()
        return VoiceQuotaClaim(
            lease_id=lease_id,
            daily_warning=global_count == ceil(settings.voice_global_per_day * 0.8),
        )
    except Exception:
        db.rollback()
        raise


def release_voice_lease(db: Session, lease_id: str | None) -> None:
    if not lease_id:
        return
    db.execute(delete(VoiceConcurrencyLease).where(VoiceConcurrencyLease.id == lease_id))
    db.commit()
