from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import timedelta
from uuid import UUID

from fastapi import Request
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.services import add_audit, stable_hash
from app.domain.speech import get_speech_transcriber, preflight_speech_provider, validate_wav
from app.domain.voice_limits import (
    VoiceQuotaClaim,
    anonymous_subject_hash,
    authenticated_subject_hash,
    claim_voice_quota,
    release_voice_lease,
)
from app.shared.config import get_settings
from app.shared.errors import BusinessError
from app.shared.models import VoiceTranscriptionRequest, utcnow
from app.shared.security import as_utc

VOICE_REPLAY_TTL = timedelta(minutes=10)
VOICE_FAILED_TTL = timedelta(days=1)


def _validated_uuid(value: str, field: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            "VALIDATION_ERROR",
            f"{field} 必须是有效 UUID",
            status_code=422,
            details={"field": field},
        ) from exc


def cleanup_expired_voice_requests(db: Session, *, now=None, commit: bool = True) -> dict[str, int]:
    current = now or utcnow()
    expired_processing = db.execute(
        update(VoiceTranscriptionRequest)
        .where(
            VoiceTranscriptionRequest.state == "PROCESSING",
            VoiceTranscriptionRequest.claim_expires_at <= current,
        )
        .values(
            state="FAILED",
            status_code=409,
            error_code="IDEMPOTENCY_RECOVERY_REQUIRED",
            transcript=None,
            claim_expires_at=None,
            replay_expires_at=current + VOICE_FAILED_TTL,
            completed_at=current,
        )
    ).rowcount
    deleted = db.execute(
        delete(VoiceTranscriptionRequest).where(
            VoiceTranscriptionRequest.state.in_(("COMPLETED", "FAILED")),
            VoiceTranscriptionRequest.replay_expires_at <= current,
        )
    ).rowcount
    if commit:
        db.commit()
    db.expire_all()
    return {"expired_processing": expired_processing, "deleted": deleted}


def _find_request(
    db: Session,
    request_scope: str,
    subject_hash: str,
    idempotency_key: str,
) -> VoiceTranscriptionRequest | None:
    return db.query(VoiceTranscriptionRequest).filter_by(
        request_scope=request_scope,
        subject_hash=subject_hash,
        idempotency_key=idempotency_key,
    ).one_or_none()


def _existing_result(
    db: Session,
    item: VoiceTranscriptionRequest,
    request_hash: str,
) -> dict[str, object]:
    if item.request_hash != request_hash:
        raise BusinessError("IDEMPOTENCY_CONFLICT", "同一个幂等键不能用于不同录音", status_code=409)
    now = utcnow()
    if item.state == "PROCESSING":
        if item.claim_expires_at is not None and as_utc(item.claim_expires_at) <= now:
            item.state = "FAILED"
            item.status_code = 409
            item.error_code = "IDEMPOTENCY_RECOVERY_REQUIRED"
            item.claim_expires_at = None
            item.replay_expires_at = now + VOICE_FAILED_TTL
            item.completed_at = now
            db.commit()
            raise BusinessError(
                "IDEMPOTENCY_RECOVERY_REQUIRED",
                "上次处理结果无法确认，为避免重复计费，请重新录音并使用新的请求编号",
                status_code=409,
            )
        raise BusinessError(
            "IDEMPOTENCY_IN_PROGRESS",
            "该录音正在处理，请稍后重试",
            status_code=409,
            details={"retry_after_seconds": get_settings().voice_lease_seconds},
        )
    if item.state == "FAILED":
        details = (
            {"retry_after_seconds": get_settings().voice_lease_seconds}
            if item.status_code == 429
            else None
        )
        raise BusinessError(
            item.error_code or "TRANSCRIPTION_FAILED",
            "该录音请求此前已失败，不会使用相同幂等键重复调用转写服务",
            status_code=item.status_code or 502,
            details=details,
        )
    if item.replay_expires_at is None or as_utc(item.replay_expires_at) <= now or not item.transcript:
        raise BusinessError(
            "IDEMPOTENCY_REPLAY_EXPIRED",
            "该录音的短时重放结果已过期，请重新录音并使用新的请求编号",
            status_code=409,
        )
    return {
        "transcript": item.transcript,
        "language": "zh-CN",
        "duration_seconds": item.duration_seconds,
        "cached": True,
    }


def _claim_request(
    db: Session,
    *,
    request_scope: str,
    subject_hash: str,
    idempotency_key: str,
    client_request_id: str,
    request_hash: str,
    duration_seconds: float,
) -> tuple[VoiceTranscriptionRequest | None, dict[str, object] | None]:
    existing = _find_request(db, request_scope, subject_hash, idempotency_key)
    if existing is not None:
        return None, _existing_result(db, existing, request_hash)
    item = VoiceTranscriptionRequest(
        request_scope=request_scope,
        subject_hash=subject_hash,
        idempotency_key=idempotency_key,
        client_request_id=client_request_id,
        request_hash=request_hash,
        state="PROCESSING",
        status_code=0,
        duration_seconds=duration_seconds,
        claim_expires_at=utcnow() + timedelta(seconds=get_settings().voice_lease_seconds * 2),
    )
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError:
        db.expire_all()
        existing = _find_request(db, request_scope, subject_hash, idempotency_key)
        if existing is None:
            raise
        return None, _existing_result(db, existing, request_hash)
    return item, None


def _best_effort_release(db: Session, lease_id: str | None) -> None:
    try:
        release_voice_lease(db, lease_id)
    except Exception:
        # Lease expiry guarantees recovery even if the release transaction fails.
        db.rollback()


async def transcribe_voice_request(
    db: Session,
    request: Request,
    *,
    audio_bytes: bytes,
    content_type: str,
    claimed_duration_seconds: float | None,
    client_request_id: str,
    idempotency_key: str,
    public: bool,
    actor_user_id: str | None = None,
) -> dict[str, object]:
    settings = get_settings()
    if not settings.voice_assistant_enabled:
        raise BusinessError("VOICE_DISABLED", "语音问答当前未启用，文字和预设问题仍可使用", status_code=503)
    if public and not settings.voice_public_enabled:
        raise BusinessError("VOICE_DISABLED", "匿名语音问答当前未启用，文字和预设问题仍可使用", status_code=503)

    normalized_request_id = _validated_uuid(client_request_id, "client_request_id")
    normalized_key = _validated_uuid(idempotency_key, "Idempotency-Key")
    if normalized_request_id != normalized_key:
        raise BusinessError("VALIDATION_ERROR", "client_request_id 必须与 Idempotency-Key 相同", status_code=422)
    audio = validate_wav(audio_bytes, content_type, claimed_duration_seconds, settings)
    subject_hash = (
        anonymous_subject_hash(request)
        if public
        else authenticated_subject_hash(actor_user_id or "unknown")
    )
    request_scope = "PUBLIC" if public else "AUTHENTICATED"
    request_hash = stable_hash(
        {
            "audio_sha256": hashlib.sha256(audio.content).hexdigest(),
            "duration_seconds": audio.duration_seconds,
            "client_request_id": normalized_request_id,
            "subject_hash": subject_hash,
        }
    )
    cleanup_expired_voice_requests(db, commit=False)
    existing = _find_request(db, request_scope, subject_hash, normalized_key)
    if existing is not None:
        replay = _existing_result(db, existing, request_hash)
        db.commit()
        return replay
    try:
        preflight_speech_provider(settings)
    except BusinessError:
        db.rollback()
        raise
    item, replay = _claim_request(
        db,
        request_scope=request_scope,
        subject_hash=subject_hash,
        idempotency_key=normalized_key,
        client_request_id=normalized_request_id,
        request_hash=request_hash,
        duration_seconds=audio.duration_seconds,
    )
    if replay is not None:
        db.commit()
        return replay
    assert item is not None

    claim: VoiceQuotaClaim
    try:
        claim = claim_voice_quota(db, subject_hash, normalized_request_id)
        if claim.daily_warning:
            try:
                add_audit(
                    db,
                    request.state.trace_id,
                    None,
                    "VOICE_DAILY_BUDGET_WARNING",
                    "voice_quota",
                    "global",
                    None,
                    {"threshold_pct": 80},
                )
                db.commit()
            except Exception:
                db.rollback()
    except Exception:
        db.rollback()
        raise

    transcription_started = time.perf_counter()
    try:
        transcript = await asyncio.wait_for(
            get_speech_transcriber().transcribe(audio),
            timeout=settings.voice_lease_seconds - 5,
        )
    except Exception as exc:
        transcription_latency_ms = round((time.perf_counter() - transcription_started) * 1_000, 2)
        error_code = exc.code if isinstance(exc, BusinessError) else "TRANSCRIPTION_FAILED"
        status_code = exc.status_code if isinstance(exc, BusinessError) else 502
        item.state = "FAILED"
        item.status_code = status_code
        item.error_code = error_code
        item.transcript = None
        item.claim_expires_at = None
        item.replay_expires_at = utcnow() + VOICE_FAILED_TTL
        item.completed_at = utcnow()
        try:
            add_audit(
                db,
                request.state.trace_id,
                actor_user_id,
                "VOICE_TRANSCRIPTION_FAILED",
                "voice_transcription",
                normalized_request_id,
                None,
                {
                    "subject_hash": subject_hash,
                    "audio_bytes": len(audio.content),
                    "duration_seconds": audio.duration_seconds,
                    "status": "FAILED",
                    "error_code": error_code,
                    "transcription_latency_ms": transcription_latency_ms,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            _best_effort_release(db, claim.lease_id)
            raise BusinessError(
                "TRANSCRIPTION_RESULT_UNAVAILABLE",
                "转写结果状态保存失败；为避免重复计费，请使用新的请求编号联系管理员",
                status_code=503,
            ) from None
        _best_effort_release(db, claim.lease_id)
        if isinstance(exc, BusinessError):
            raise
        raise BusinessError("TRANSCRIPTION_FAILED", "语音转写服务暂时不可用", status_code=502) from None

    transcription_latency_ms = round((time.perf_counter() - transcription_started) * 1_000, 2)
    item.state = "COMPLETED"
    item.status_code = 200
    item.transcript = transcript
    item.transcript_sha256 = hashlib.sha256(transcript.encode()).hexdigest()
    item.error_code = None
    item.claim_expires_at = None
    item.replay_expires_at = utcnow() + VOICE_REPLAY_TTL
    item.completed_at = utcnow()
    try:
        add_audit(
            db,
            request.state.trace_id,
            actor_user_id,
            "VOICE_TRANSCRIPTION_COMPLETED",
            "voice_transcription",
            normalized_request_id,
            None,
            {
                "subject_hash": subject_hash,
                "audio_bytes": len(audio.content),
                "duration_seconds": audio.duration_seconds,
                "status": "COMPLETED",
                "transcription_latency_ms": transcription_latency_ms,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        _best_effort_release(db, claim.lease_id)
        raise BusinessError(
            "TRANSCRIPTION_RESULT_UNAVAILABLE",
            "转写结果状态保存失败；为避免重复计费，请使用新的请求编号联系管理员",
            status_code=503,
        ) from None
    _best_effort_release(db, claim.lease_id)
    return {
        "transcript": transcript,
        "language": "zh-CN",
        "duration_seconds": audio.duration_seconds,
        "cached": False,
    }
