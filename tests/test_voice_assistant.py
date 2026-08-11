from __future__ import annotations

import asyncio
import io
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.b01.main import app as b01_app
from app.domain import assistant as assistant_domain
from app.domain import speech as speech_domain
from app.domain import voice_service
from app.domain.speech import (
    AliyunNlsTokenManager,
    AliyunNlsTranscriber,
    preflight_speech_provider,
    validate_wav,
)
from app.domain.voice_limits import (
    _shanghai_day_window,
    claim_public_text_quota,
    claim_voice_quota,
    release_voice_lease,
)
from app.domain.voice_service import cleanup_expired_voice_requests
from app.shared.config import Settings, get_settings
from app.shared.database import SessionLocal, engine
from app.shared.errors import BusinessError
from app.shared.models import AuditLog, VoiceConcurrencyLease, VoiceQuotaBucket, VoiceTranscriptionRequest


def wav_bytes(*, seconds: float = 0.2, sample_rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(sample_rate)
        target.writeframes(b"\x00" * int(seconds * sample_rate) * channels * width)
    return buffer.getvalue()


class FakeTranscriber:
    def __init__(self, transcript: str = "本月第三空间营业额是多少？", error: BusinessError | None = None) -> None:
        self.transcript = transcript
        self.error = error
        self.calls = 0

    async def transcribe(self, _audio) -> str:
        self.calls += 1
        if self.error:
            raise self.error
        return self.transcript


@pytest.fixture
def enabled_voice(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "voice_assistant_enabled", True)
    monkeypatch.setattr(settings, "voice_public_enabled", True)
    monkeypatch.setattr(settings, "assistant_public_db_quota_enabled", True)
    monkeypatch.setattr(settings, "voice_public_per_minute", 100)
    monkeypatch.setattr(settings, "voice_public_per_hour", 100)
    monkeypatch.setattr(settings, "voice_public_per_day", 100)
    monkeypatch.setattr(settings, "voice_public_text_per_minute", 100)
    monkeypatch.setattr(settings, "voice_public_text_per_day", 100)
    monkeypatch.setattr(settings, "voice_global_per_day", 1_000)
    monkeypatch.setattr(settings, "voice_max_concurrency", 10)
    monkeypatch.setattr(settings, "aliyun_nls_access_key_id", SecretStr("unit-test-access-key"))
    monkeypatch.setattr(settings, "aliyun_nls_access_key_secret", SecretStr("unit-test-access-secret"))
    monkeypatch.setattr(settings, "aliyun_nls_app_key", SecretStr("unit-test-app-key"))
    with SessionLocal() as db:
        db.execute(delete(VoiceConcurrencyLease))
        db.execute(delete(VoiceQuotaBucket))
        db.execute(delete(VoiceTranscriptionRequest))
        db.execute(delete(AuditLog).where(AuditLog.action.like("%ASSISTANT%")))
        db.execute(delete(AuditLog).where(AuditLog.action.like("VOICE_%")))
        db.commit()
    return settings


def test_wav_validation_checks_real_container_and_frames():
    payload = wav_bytes(seconds=0.25)
    parsed = validate_wav(payload, "audio/wav", 0.25)
    assert parsed.sample_rate == 16_000
    assert parsed.channels == 1
    assert parsed.sample_width_bits == 16
    assert parsed.duration_seconds == 0.25

    with pytest.raises(BusinessError, match="RIFF"):
        validate_wav(payload + b"unexpected-tail", "audio/wav", 0.25)
    with pytest.raises(BusinessError) as wrong_rate:
        validate_wav(wav_bytes(sample_rate=44_100), "audio/wav", 0.2)
    assert wrong_rate.value.code == "AUDIO_INVALID"
    with pytest.raises(BusinessError):
        validate_wav(wav_bytes(channels=2), "audio/wav", 0.2)
    with pytest.raises(BusinessError):
        validate_wav(wav_bytes(width=1), "audio/wav", 0.2)
    with pytest.raises(BusinessError) as too_long:
        validate_wav(wav_bytes(seconds=30.1), "audio/wav", 30)
    assert too_long.value.code == "AUDIO_INVALID"
    with pytest.raises(BusinessError) as too_large:
        validate_wav(b"RIFF" + b"\x00" * (2 * 1024 * 1024), "audio/wav", 1)
    assert too_large.value.code == "AUDIO_TOO_LARGE"
    with pytest.raises(BusinessError) as invalid_duration:
        validate_wav(payload, "audio/wav", float("nan"))
    assert invalid_duration.value.code == "AUDIO_INVALID"
    with pytest.raises(BusinessError) as wrong_type:
        validate_wav(payload, "audio/webm", 0.25)
    assert wrong_type.value.code == "AUDIO_TYPE_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_aliyun_token_cache_and_rest_parameters(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "aliyun_nls_app_key", SecretStr("test-app-key"))
    monkeypatch.setattr(settings, "aliyun_nls_vocabulary_id", "business-words")
    manager = AliyunNlsTokenManager(settings)
    calls = 0

    def fetch_token() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        return "cached-token", int(time.time()) + 3_600

    monkeypatch.setattr(manager, "_fetch_token_sync", fetch_token)
    assert await asyncio.gather(manager.get_token(), manager.get_token()) == ["cached-token", "cached-token"]
    assert calls == 1

    manager._token = "expired-token"
    manager._expires_at = int(time.time()) + 3_600
    refreshed = await asyncio.gather(
        manager.refresh_expired("expired-token"),
        manager.refresh_expired("expired-token"),
    )
    assert refreshed == ["cached-token", "cached-token"]
    assert calls == 2

    captured: dict = {}

    class Response:
        status_code = 200
        request = httpx.Request("POST", settings.aliyun_nls_endpoint)

        @staticmethod
        def json() -> dict:
            return {"status": 20_000_000, "result": "第三空间营业额"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(speech_domain.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = await AliyunNlsTranscriber(settings, manager).transcribe(
        validate_wav(wav_bytes(), "audio/wav", 0.2)
    )
    assert result == "第三空间营业额"
    assert captured["headers"]["X-NLS-Token"] == "cached-token"
    assert captured["params"] == {
        "appkey": "test-app-key",
        "format": "wav",
        "sample_rate": 16_000,
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
        "enable_voice_detection": "true",
        "vocabulary_id": "business-words",
    }
    assert captured["content"].startswith(b"RIFF")


@pytest.mark.asyncio
async def test_two_token_expired_responses_refresh_only_once(monkeypatch):
    settings = get_settings()
    manager = AliyunNlsTokenManager(settings)
    manager._token = "rejected-token"
    manager._expires_at = int(time.time()) + 3_600
    refresh_calls = 0

    def fetch_token() -> tuple[str, int]:
        nonlocal refresh_calls
        refresh_calls += 1
        return "replacement-token", int(time.time()) + 3_600

    monkeypatch.setattr(manager, "_fetch_token_sync", fetch_token)
    transcriber = AliyunNlsTranscriber(settings, manager)

    async def request(token, _audio):
        await asyncio.sleep(0.01)
        if token == "rejected-token":
            return {"status": 40_000_001}
        return {"status": 20_000_000, "result": "转写成功"}

    monkeypatch.setattr(transcriber, "_request", request)
    audio = validate_wav(wav_bytes(), "audio/wav", 0.2)
    assert await asyncio.gather(transcriber.transcribe(audio), transcriber.transcribe(audio)) == [
        "转写成功",
        "转写成功",
    ]
    assert refresh_calls == 1


def _voice_post(client, request_id: str, payload: bytes | None = None):
    return client.post(
        "/api/v1/public/assistant/transcriptions",
        headers={"Idempotency-Key": request_id},
        data={"duration_seconds": "0.2", "client_request_id": request_id},
        files={"audio": ("question.wav", payload or wav_bytes(), "audio/wav")},
    )


def test_public_voice_success_shared_replay_and_private_audit(
    b01_client,
    enabled_voice,
    monkeypatch,
):
    fake = FakeTranscriber()
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    request_id = str(uuid4())
    first = _voice_post(b01_client, request_id)
    second = _voice_post(b01_client, request_id)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["transcript"] == fake.transcript
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert fake.calls == 1

    with SessionLocal() as db:
        record = db.scalar(
            select(VoiceTranscriptionRequest).where(
                VoiceTranscriptionRequest.idempotency_key == request_id,
            )
        )
        audits = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "VOICE_TRANSCRIPTION_COMPLETED"))
        )
    assert record is not None and record.state == "COMPLETED"
    assert record.transcript == fake.transcript
    assert len(audits) == 1
    assert "transcript" not in audits[0].after_snapshot
    assert "audio" not in audits[0].after_snapshot
    assert audits[0].after_snapshot["transcription_latency_ms"] >= 0


def test_authenticated_voice_keeps_optional_legacy_fields(
    b01_client,
    admin_headers,
    enabled_voice,
    monkeypatch,
):
    fake = FakeTranscriber("库存情况")
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    response = b01_client.post(
        "/api/v1/web/assistant/transcriptions",
        headers=admin_headers,
        files={"audio": ("question.wav", wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["transcript"] == "库存情况"
    assert fake.calls == 1


def test_valid_thirty_second_upload_remains_in_memory(
    b01_client,
    enabled_voice,
    monkeypatch,
):
    fake = FakeTranscriber("长录音通过")
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    request_id = str(uuid4())
    response = b01_client.post(
        "/api/v1/public/assistant/transcriptions",
        headers={"Idempotency-Key": request_id},
        data={"duration_seconds": "30", "client_request_id": request_id},
        files={"audio": ("question.wav", wav_bytes(seconds=30), "audio/wav")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["duration_seconds"] == 30


def test_failed_provider_call_is_not_repeated_for_same_key(b01_client, enabled_voice, monkeypatch):
    fake = FakeTranscriber(error=BusinessError("TRANSCRIPTION_FAILED", "不可用", status_code=502))
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    request_id = str(uuid4())
    first = _voice_post(b01_client, request_id)
    second = _voice_post(b01_client, request_id)
    assert first.status_code == 502
    assert second.status_code == 502
    assert fake.calls == 1


def test_provider_429_replay_keeps_retry_after(b01_client, enabled_voice, monkeypatch):
    fake = FakeTranscriber(
        error=BusinessError(
            "VOICE_CONCURRENCY_EXCEEDED",
            "当前并发已满",
            status_code=429,
            details={"retry_after_seconds": enabled_voice.voice_lease_seconds},
        )
    )
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    request_id = str(uuid4())
    first = _voice_post(b01_client, request_id)
    replay = _voice_post(b01_client, request_id)
    assert first.status_code == replay.status_code == 429
    assert first.headers["Retry-After"] == replay.headers["Retry-After"]
    assert replay.json()["details"]["retry_after_seconds"] == enabled_voice.voice_lease_seconds
    assert fake.calls == 1


def test_provider_diagnostics_do_not_leak_to_response_or_audit(
    b01_client,
    enabled_voice,
    monkeypatch,
):
    sentinel = "never-log-appkey-token-ak"
    fake = FakeTranscriber(error=RuntimeError(sentinel))
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    response = _voice_post(b01_client, str(uuid4()))
    assert response.status_code == 502
    assert sentinel not in response.text
    with SessionLocal() as db:
        audits = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "VOICE_TRANSCRIPTION_FAILED"))
        )
    assert audits
    assert sentinel not in str(audits[-1].after_snapshot)


def test_voice_rate_limit_has_retry_after_header(b01_client, enabled_voice, monkeypatch):
    monkeypatch.setattr(enabled_voice, "voice_public_per_minute", 1)
    fake = FakeTranscriber()
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    assert _voice_post(b01_client, str(uuid4())).status_code == 200
    limited = _voice_post(b01_client, str(uuid4()))
    assert limited.status_code == 429
    assert limited.json()["code"] == "VOICE_RATE_LIMITED"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert int(limited.headers["Retry-After"]) == limited.json()["details"]["retry_after_seconds"]


def test_transcription_content_length_is_rejected_before_form_parsing(b01_client):
    settings = get_settings()
    response = b01_client.post(
        "/api/v1/public/assistant/transcriptions",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(settings.voice_max_bytes + 128 * 1024 + 1),
        },
        content=b"ignored",
    )
    assert response.status_code == 413
    assert response.json()["code"] == "AUDIO_TOO_LARGE"


def test_missing_voice_secrets_fail_endpoint_closed_without_breaking_settings(
    b01_client,
    enabled_voice,
    monkeypatch,
):
    standalone = Settings(
        _env_file=None,
        voice_assistant_enabled=True,
        voice_public_enabled=True,
        assistant_public_db_quota_enabled=True,
        aliyun_nls_access_key_id=None,
        aliyun_nls_access_key_secret=None,
        aliyun_nls_app_key=None,
        voice_rate_limit_hmac_secret=None,
    )
    assert standalone.voice_assistant_enabled is True
    monkeypatch.setattr(enabled_voice, "voice_rate_limit_hmac_secret", None)
    response = _voice_post(b01_client, str(uuid4()))
    assert response.status_code == 503
    assert response.json()["code"] == "TRANSCRIPTION_NOT_CONFIGURED"
    assert b01_client.get("/health/b01").status_code == 200


def test_provider_preflight_failure_does_not_claim_quota_or_replay(
    b01_client,
    enabled_voice,
    monkeypatch,
):
    monkeypatch.setattr(enabled_voice, "aliyun_nls_app_key", SecretStr(""))
    response = _voice_post(b01_client, str(uuid4()))
    assert response.status_code == 503
    assert response.json()["code"] == "TRANSCRIPTION_NOT_CONFIGURED"
    with SessionLocal() as db:
        assert db.scalar(select(VoiceQuotaBucket).limit(1)) is None
        assert db.scalar(select(VoiceConcurrencyLease).limit(1)) is None
        assert db.scalar(select(VoiceTranscriptionRequest).limit(1)) is None


def test_completed_replay_does_not_require_current_provider_configuration(
    b01_client,
    enabled_voice,
    monkeypatch,
):
    fake = FakeTranscriber("已完成的短时转写")
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    request_id = str(uuid4())
    first = _voice_post(b01_client, request_id)
    assert first.status_code == 200
    with SessionLocal() as db:
        quota_count = list(db.scalars(select(VoiceQuotaBucket)))

    monkeypatch.setattr(enabled_voice, "aliyun_nls_app_key", SecretStr(""))
    replay = _voice_post(b01_client, request_id)
    assert replay.status_code == 200, replay.text
    assert replay.json()["cached"] is True
    assert replay.json()["transcript"] == "已完成的短时转写"
    assert fake.calls == 1
    with SessionLocal() as db:
        assert len(list(db.scalars(select(VoiceQuotaBucket)))) == len(quota_count)


@pytest.mark.asyncio
async def test_empty_aliyun_secret_values_are_not_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "aliyun_nls_access_key_id", SecretStr(""))
    monkeypatch.setattr(settings, "aliyun_nls_access_key_secret", SecretStr("   "))
    monkeypatch.setattr(settings, "aliyun_nls_app_key", SecretStr(""))
    manager = AliyunNlsTokenManager(settings)
    with pytest.raises(BusinessError) as token_error:
        manager._fetch_token_sync()
    assert token_error.value.code == "TRANSCRIPTION_NOT_CONFIGURED"
    assert token_error.value.status_code == 503

    transcriber = AliyunNlsTranscriber(settings, manager)
    with pytest.raises(BusinessError) as app_key_error:
        await transcriber._request("token", validate_wav(wav_bytes(), "audio/wav", 0.2))
    assert app_key_error.value.code == "TRANSCRIPTION_NOT_CONFIGURED"
    assert app_key_error.value.status_code == 503


def test_provider_preflight_checks_missing_sdk_and_openai_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "aliyun_nls_access_key_id", SecretStr("configured-id"))
    monkeypatch.setattr(settings, "aliyun_nls_access_key_secret", SecretStr("configured-secret"))
    monkeypatch.setattr(settings, "aliyun_nls_app_key", SecretStr("configured-app"))
    monkeypatch.setattr(speech_domain.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(BusinessError) as sdk_error:
        preflight_speech_provider(settings)
    assert sdk_error.value.code == "TRANSCRIPTION_NOT_CONFIGURED"

    monkeypatch.setattr(settings, "voice_stt_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(BusinessError) as openai_error:
        preflight_speech_provider(settings)
    assert openai_error.value.code == "TRANSCRIPTION_NOT_CONFIGURED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_payload", "expected_code"),
    [
        ({"status": 20_000_000, "result": ""}, "TRANSCRIPTION_EMPTY"),
        ({"status": 40_000_005, "result": ""}, "VOICE_CONCURRENCY_EXCEEDED"),
        ({"status": 50_000_000, "result": ""}, "TRANSCRIPTION_FAILED"),
    ],
)
async def test_aliyun_provider_error_mapping(provider_payload, expected_code, monkeypatch):
    settings = get_settings()
    manager = AliyunNlsTokenManager(settings)
    monkeypatch.setattr(manager, "get_token", lambda: asyncio.sleep(0, result="token"))
    transcriber = AliyunNlsTranscriber(settings, manager)

    async def request(_token, _audio):
        return provider_payload

    monkeypatch.setattr(transcriber, "_request", request)
    with pytest.raises(BusinessError) as captured:
        await transcriber.transcribe(validate_wav(wav_bytes(), "audio/wav", 0.2))
    assert captured.value.code == expected_code


def _assert_no_internal_keys(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert not key.endswith("_id"), key
            assert key not in {
                "id",
                "object_version",
                "token",
                "input_snapshot",
                "output_snapshot",
                "order_versions",
            }
            _assert_no_internal_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_internal_keys(item)


def test_public_assistant_uses_period_local_intent_and_whitelisted_dtos(
    b01_client,
    enabled_voice,
    monkeypatch,
):
    def forbidden_external(_question: str):
        raise AssertionError("公开问答不得调用外部意图分类")

    monkeypatch.setattr(assistant_domain, "classify_intent", forbidden_external)
    questions = (
        "园区概览",
        "第三空间渠道占比",
        "当前报警",
        "经营营业额",
        "库存缺货",
        "运输车辆位置",
        "拼车利用率",
        "拼仓库容",
        "采购供应商",
        "下周需求预测",
    )
    responses = {}
    for question in questions:
        response = b01_client.post(
            "/api/v1/public/assistant/query",
            json={"question": question, "preferred_chart": "auto", "period": "7d"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["period"] == "7d"
        assert body["mode"] == "deterministic"
        assert "generated_at" in body
        assert "data_cutoff" in body
        _assert_no_internal_keys(body["chart"]["data"])
        responses[body["intent"]] = body

    assert responses["TRANSPORT"]["chart"]["data"]
    assert all("alerts" in route for route in responses["TRANSPORT"]["chart"]["data"])
    assert all(
        route["alert_source"] == "独立温湿度报警投影"
        for route in responses["TRANSPORT"]["chart"]["data"]
    )
    if responses["FORECAST"]["chart"]["data"]:
        assert "suggested_purchase_quantity" in responses["FORECAST"]["chart"]["data"][0]
    if responses["CARPOOL"]["data_cutoff"] is None:
        assert responses["CARPOOL"]["data_cutoff_note"]

    blocked = b01_client.post(
        "/api/v1/public/assistant/query",
        json={"question": "请执行 select * from users", "period": "30d"},
    )
    assert blocked.status_code == 400
    assert blocked.json()["code"] == "ASSISTANT_QUERY_NOT_ALLOWED"

    fallback = b01_client.post(
        "/api/v1/public/assistant/query",
        json={"question": "车辆运输路线", "preferred_chart": "bar", "period": "30d"},
    )
    assert fallback.status_code == 200
    assert fallback.json()["chart"]["type"] == "route"
    assert fallback.json()["chart_fallback_reason"]

    period_payloads = {
        period: b01_client.post(
            "/api/v1/public/assistant/query",
            json={"question": "经营营业额", "period": period},
        ).json()
        for period in ("7d", "30d", "month")
    }
    assert len({payload["period_start"] for payload in period_payloads.values()}) == 3
    assert period_payloads["7d"]["chart"]["data"] != period_payloads["30d"]["chart"]["data"]

    with SessionLocal() as db:
        completed_audits = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "PUBLIC_ASSISTANT_QUERY",
                    AuditLog.after_snapshot["status"].as_string() == "COMPLETED",
                )
            )
        )
    assert completed_audits
    metadata = completed_audits[-1].after_snapshot
    assert metadata["assistant_latency_ms"] >= 0
    assert "data_cutoff" in metadata
    for forbidden in ("question", "transcript", "audio", "audio_content"):
        assert forbidden not in str(metadata).lower()


def test_rejected_assistant_query_is_audited_without_content(b01_client, enabled_voice):
    marker = "select-secret-question-marker"
    response = b01_client.post(
        "/api/v1/public/assistant/query",
        json={"question": f"请执行 select * from users {marker}", "period": "month"},
    )
    assert response.status_code == 400
    with SessionLocal() as db:
        audit = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == "PUBLIC_ASSISTANT_QUERY")
            .order_by(AuditLog.created_at.desc())
        )
    assert audit is not None
    assert audit.after_snapshot["status"] == "FAILED"
    assert audit.after_snapshot["error_code"] == "ASSISTANT_QUERY_NOT_ALLOWED"
    assert audit.after_snapshot["period"] == "month"
    assert marker not in str(audit.after_snapshot)


def test_assistant_reads_real_forecast_and_procurement_projections(
    b01_client,
    admin_headers,
    enabled_voice,
):
    forecast_generated = b01_client.post(
        "/api/v1/web/forecasts/next-week/generate",
        headers={**admin_headers, "Idempotency-Key": f"voice-forecast-{uuid4()}"},
    )
    assert forecast_generated.status_code == 201, forecast_generated.text

    from app.domain.procurement import procurement_cycle

    cycle_start, _ = procurement_cycle()
    procurement_generated = b01_client.post(
        "/api/v1/web/procurement/aggregations/generate",
        headers={**admin_headers, "Idempotency-Key": f"voice-procurement-{uuid4()}"},
        json={"cycle_start": cycle_start.isoformat()},
    )
    assert procurement_generated.status_code == 201, procurement_generated.text

    forecast = b01_client.post(
        "/api/v1/public/assistant/query",
        json={"question": "下周需求预测和建议采购量", "period": "7d"},
    )
    assert forecast.status_code == 200, forecast.text
    forecast_body = forecast.json()
    assert forecast_body["intent"] == "FORECAST"
    assert forecast_body["chart"]["data"]
    assert all("suggested_purchase_quantity" in row for row in forecast_body["chart"]["data"])
    assert forecast_body["data_cutoff"] == forecast_generated.json()["data_cutoff"]
    _assert_no_internal_keys(forecast_body["chart"]["data"])

    procurement = b01_client.post(
        "/api/v1/public/assistant/query",
        json={"question": "本周采购推荐和供应商方案", "period": "7d"},
    )
    assert procurement.status_code == 200, procurement.text
    procurement_body = procurement.json()
    assert procurement_body["intent"] == "PROCUREMENT"
    assert procurement_body["chart"]["data"]
    assert all("automatic_quantity" in row for row in procurement_body["chart"]["data"])
    assert all("supplier_name" in row for row in procurement_body["chart"]["data"])
    _assert_no_internal_keys(procurement_body["chart"]["data"])


def test_quota_windows_are_atomic_and_use_shanghai_day(enabled_voice, monkeypatch):
    monkeypatch.setattr(enabled_voice, "voice_public_text_per_minute", 100)
    monkeypatch.setattr(enabled_voice, "voice_public_text_per_day", 1)
    with SessionLocal() as db:
        db.execute(delete(VoiceQuotaBucket))
        db.commit()
        claim_public_text_quota(db, "c" * 64)
        with pytest.raises(BusinessError) as captured:
            claim_public_text_quota(db, "c" * 64)
        assert captured.value.code == "VOICE_RATE_LIMITED"
        minute = db.scalar(
            select(VoiceQuotaBucket).where(VoiceQuotaBucket.scope == "public-text-minute")
        )
        assert minute is not None and minute.request_count == 1

    before = _shanghai_day_window(datetime(2026, 8, 11, 15, 59, tzinfo=UTC))
    after = _shanghai_day_window(datetime(2026, 8, 11, 16, 0, tzinfo=UTC))
    assert after - before == timedelta(days=1)

    just_before_midnight = datetime(2026, 8, 11, 15, 59, 59, tzinfo=UTC)
    with SessionLocal() as db:
        db.execute(delete(VoiceQuotaBucket))
        db.commit()
        claim_public_text_quota(db, "9" * 64, now=just_before_midnight)
        with pytest.raises(BusinessError) as day_limit:
            claim_public_text_quota(db, "9" * 64, now=just_before_midnight)
    assert day_limit.value.details["retry_after_seconds"] == 1


def test_nginx_voice_locations_disable_request_buffering():
    config = (Path(__file__).parents[1] / "deploy/nginx/black-soil-loop.conf").read_text(encoding="utf-8")
    assert "include proxy_params" not in config
    for path in (
        "/api/v1/public/assistant/transcriptions",
        "/api/v1/web/assistant/transcriptions",
    ):
        block = config.split(f"location = {path} {{", maxsplit=1)[1].split("}", maxsplit=1)[0]
        assert "client_max_body_size 2200k;" in block
        assert "client_body_buffer_size 2200k;" in block
        assert "proxy_request_buffering off;" in block
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in block


def test_voice_replay_cleanup_expires_content_and_stale_processing(enabled_voice):
    now = datetime.now(UTC)
    with SessionLocal() as db:
        completed = VoiceTranscriptionRequest(
            request_scope="PUBLIC",
            subject_hash="d" * 64,
            idempotency_key=str(uuid4()),
            client_request_id=str(uuid4()),
            request_hash="e" * 64,
            state="COMPLETED",
            status_code=200,
            transcript="不应保留",
            transcript_sha256="f" * 64,
            duration_seconds=1,
            replay_expires_at=now - timedelta(seconds=1),
            completed_at=now - timedelta(minutes=11),
        )
        stale = VoiceTranscriptionRequest(
            request_scope="PUBLIC",
            subject_hash="a" * 64,
            idempotency_key=str(uuid4()),
            client_request_id=str(uuid4()),
            request_hash="b" * 64,
            state="PROCESSING",
            status_code=0,
            duration_seconds=1,
            claim_expires_at=now - timedelta(seconds=1),
        )
        db.add_all([completed, stale])
        db.commit()
        completed_id = completed.id
        stale_id = stale.id
        result = cleanup_expired_voice_requests(db, now=now)
        assert result == {"expired_processing": 1, "deleted": 1}
        assert db.get(VoiceTranscriptionRequest, completed_id) is None
        recovered = db.get(VoiceTranscriptionRequest, stale_id)
        assert recovered is not None
        assert recovered.state == "FAILED"
        assert recovered.transcript is None
        assert recovered.error_code == "IDEMPOTENCY_RECOVERY_REQUIRED"


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="需要 PostgreSQL 验证配额并发")
def test_postgres_voice_concurrency_lease_allows_exactly_one(enabled_voice, monkeypatch):
    monkeypatch.setattr(enabled_voice, "voice_max_concurrency", 1)
    barrier = Barrier(2, timeout=15)

    def claim(subject: str) -> str:
        with SessionLocal() as db:
            barrier.wait()
            try:
                result = claim_voice_quota(db, subject, str(uuid4()))
            except BusinessError as exc:
                return exc.code
            return result.lease_id or "missing"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("a" * 64, "b" * 64)))
    assert sum(result == "VOICE_CONCURRENCY_EXCEEDED" for result in results) == 1
    lease_id = next(result for result in results if result != "VOICE_CONCURRENCY_EXCEEDED")
    with SessionLocal() as db:
        release_voice_lease(db, lease_id)


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="需要 PostgreSQL 验证配额并发")
def test_postgres_last_voice_quota_allows_exactly_one(enabled_voice, monkeypatch):
    monkeypatch.setattr(enabled_voice, "voice_public_per_minute", 1)
    monkeypatch.setattr(enabled_voice, "voice_max_concurrency", 10)
    barrier = Barrier(2, timeout=15)

    def claim() -> str:
        with SessionLocal() as db:
            barrier.wait()
            try:
                result = claim_voice_quota(db, "f" * 64, str(uuid4()))
            except BusinessError as exc:
                return exc.code
            return result.lease_id or "missing"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(result == "VOICE_RATE_LIMITED" for result in results) == 1
    with SessionLocal() as db:
        for lease_id in results:
            if lease_id != "VOICE_RATE_LIMITED":
                release_voice_lease(db, lease_id)


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="需要 PostgreSQL 验证幂等并发")
def test_postgres_same_voice_key_calls_provider_once(enabled_voice, monkeypatch):
    fake = FakeTranscriber()

    async def delayed(_audio) -> str:
        fake.calls += 1
        await asyncio.sleep(0.2)
        return fake.transcript

    monkeypatch.setattr(fake, "transcribe", delayed)
    monkeypatch.setattr(voice_service, "get_speech_transcriber", lambda: fake)
    request_id = str(uuid4())
    barrier = Barrier(2, timeout=15)

    def submit() -> int:
        with TestClient(b01_app) as client:
            barrier.wait()
            return _voice_post(client, request_id).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: submit(), range(2)))
    assert any(status == 200 for status in statuses)
    assert all(status in {200, 409} for status in statuses)
    assert fake.calls == 1
