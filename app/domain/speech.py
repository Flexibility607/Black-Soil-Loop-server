from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import math
import time
import wave
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx

from app.domain.openai_layer import transcribe_audio as openai_transcribe_audio
from app.shared.config import Settings, get_settings
from app.shared.errors import BusinessError

WAV_CONTENT_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}
ALIYUN_SUCCESS = 20_000_000
ALIYUN_TOKEN_EXPIRED = 40_000_001
ALIYUN_TOO_MANY_REQUESTS = 40_000_005


@dataclass(frozen=True)
class ValidatedWav:
    content: bytes
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width_bits: int


class SpeechTranscriber(Protocol):
    async def transcribe(self, audio: ValidatedWav) -> str: ...


def preflight_speech_provider(settings: Settings | None = None) -> None:
    """Validate local provider readiness without requesting a token or calling an upstream."""
    active = settings or get_settings()
    if active.voice_stt_provider == "openai":
        if not (active.openai_api_key or "").strip():
            raise BusinessError(
                "TRANSCRIPTION_NOT_CONFIGURED",
                "OpenAI 语音转写密钥尚未配置",
                status_code=503,
            )
        return
    secrets = (
        active.aliyun_nls_access_key_id,
        active.aliyun_nls_access_key_secret,
        active.aliyun_nls_app_key,
    )
    if any(value is None or not value.get_secret_value().strip() for value in secrets):
        raise BusinessError(
            "TRANSCRIPTION_NOT_CONFIGURED",
            "阿里云语音 AccessKey、Secret 或 AppKey 尚未配置",
            status_code=503,
        )
    if importlib.util.find_spec("aliyunsdkcore") is None:
        raise BusinessError(
            "TRANSCRIPTION_NOT_CONFIGURED",
            "服务器缺少阿里云 SDK，语音转写不可用",
            status_code=503,
        )


def validate_wav(
    content: bytes,
    content_type: str,
    claimed_duration_seconds: float | None,
    settings: Settings | None = None,
) -> ValidatedWav:
    active = settings or get_settings()
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type not in WAV_CONTENT_TYPES:
        raise BusinessError("AUDIO_TYPE_NOT_ALLOWED", "仅支持 16 kHz 单声道 16 位 PCM WAV 录音", status_code=415)
    if len(content) > active.voice_max_bytes:
        raise BusinessError(
            "AUDIO_TOO_LARGE",
            f"录音文件不能超过 {active.voice_max_bytes} 字节",
            status_code=413,
        )
    if claimed_duration_seconds is not None and (
        not math.isfinite(claimed_duration_seconds) or claimed_duration_seconds <= 0
    ):
        raise BusinessError("AUDIO_INVALID", "客户端录音时长无效", status_code=422)
    if len(content) < 12 or not content.startswith(b"RIFF") or content[8:12] != b"WAVE":
        raise BusinessError("AUDIO_INVALID", "录音不是有效的 WAV 文件", status_code=422)
    if int.from_bytes(content[4:8], "little") + 8 != len(content):
        raise BusinessError("AUDIO_INVALID", "WAV 文件长度与 RIFF 头不一致", status_code=422)
    try:
        with wave.open(io.BytesIO(content), "rb") as source:
            channels = source.getnchannels()
            sample_width_bits = source.getsampwidth() * 8
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            frames = source.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise BusinessError("AUDIO_INVALID", "WAV 文件结构损坏", status_code=422) from exc
    if compression != "NONE" or channels != 1 or sample_width_bits != 16 or sample_rate != 16_000:
        raise BusinessError(
            "AUDIO_INVALID",
            "录音必须为 16 kHz、单声道、16 位 PCM WAV",
            status_code=422,
            details={
                "sample_rate": sample_rate,
                "channels": channels,
                "sample_width_bits": sample_width_bits,
            },
        )
    expected_frame_bytes = frame_count * channels * (sample_width_bits // 8)
    if len(frames) != expected_frame_bytes:
        raise BusinessError("AUDIO_INVALID", "WAV 音频帧不完整", status_code=422)
    duration = frame_count / sample_rate if sample_rate else 0
    if frame_count <= 0 or duration <= 0 or duration > active.voice_max_seconds:
        raise BusinessError(
            "AUDIO_INVALID",
            f"录音时长必须大于 0 且不超过 {active.voice_max_seconds} 秒",
            status_code=422,
        )
    tolerance = max(0.5, duration * 0.15)
    if claimed_duration_seconds is not None and abs(duration - claimed_duration_seconds) > tolerance:
        raise BusinessError(
            "AUDIO_INVALID",
            "客户端时长与 WAV 实际时长不一致",
            status_code=422,
            details={"actual_duration_seconds": round(duration, 3)},
        )
    return ValidatedWav(content, round(duration, 3), sample_rate, channels, sample_width_bits)


class AliyunNlsTokenManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: str | None = None
        self._expires_at = 0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        if self._token and self._expires_at - 300 > time.time():
            return self._token
        async with self._lock:
            if self._token and self._expires_at - 300 > time.time():
                return self._token
            token, expires_at = await asyncio.wait_for(asyncio.to_thread(self._fetch_token_sync), timeout=15)
            self._token = token
            self._expires_at = expires_at
            return token

    async def refresh_expired(self, stale_token: str) -> str:
        async with self._lock:
            if self._token and self._token != stale_token and self._expires_at - 300 > time.time():
                return self._token
            token, expires_at = await asyncio.wait_for(asyncio.to_thread(self._fetch_token_sync), timeout=15)
            self._token = token
            self._expires_at = expires_at
            return token

    def _fetch_token_sync(self) -> tuple[str, int]:
        access_key_id = self._settings.aliyun_nls_access_key_id
        access_key_secret = self._settings.aliyun_nls_access_key_secret
        if (
            access_key_id is None
            or not access_key_id.get_secret_value().strip()
            or access_key_secret is None
            or not access_key_secret.get_secret_value().strip()
        ):
            raise BusinessError("TRANSCRIPTION_NOT_CONFIGURED", "阿里云语音凭据尚未配置", status_code=503)
        try:
            from aliyunsdkcore.client import AcsClient
            from aliyunsdkcore.request import CommonRequest
        except ImportError:
            raise BusinessError(
                "TRANSCRIPTION_NOT_CONFIGURED",
                "服务器缺少阿里云 SDK，语音转写不可用",
                status_code=503,
            ) from None
        request = CommonRequest()
        request.set_method("POST")
        request.set_domain("nls-meta.cn-shanghai.aliyuncs.com")
        request.set_version("2019-02-28")
        request.set_action_name("CreateToken")
        request.set_connect_timeout(5)
        request.set_read_timeout(10)
        try:
            client = AcsClient(
                access_key_id.get_secret_value(),
                access_key_secret.get_secret_value(),
                "cn-shanghai",
            )
            raw = client.do_action_with_exception(request)
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            token = str(payload["Token"]["Id"])
            expires_at = int(payload["Token"]["ExpireTime"])
        except BusinessError:
            raise
        except (KeyError, TypeError, ValueError, RuntimeError):
            raise BusinessError("TRANSCRIPTION_FAILED", "阿里云语音鉴权暂时不可用", status_code=502) from None
        except Exception:
            # The Alibaba Cloud SDK exposes multiple ClientException variants.
            # Map all of them without leaking credential-related diagnostics.
            raise BusinessError("TRANSCRIPTION_FAILED", "阿里云语音鉴权暂时不可用", status_code=502) from None
        if not token or expires_at <= int(time.time()):
            raise BusinessError("TRANSCRIPTION_FAILED", "阿里云语音鉴权返回无效 Token", status_code=502)
        return token, expires_at


class AliyunNlsTranscriber:
    def __init__(self, settings: Settings, token_manager: AliyunNlsTokenManager | None = None) -> None:
        self._settings = settings
        self._tokens = token_manager or AliyunNlsTokenManager(settings)

    async def transcribe(self, audio: ValidatedWav) -> str:
        token = await self._tokens.get_token()
        payload = await self._request(token, audio)
        if int(payload.get("status") or 0) == ALIYUN_TOKEN_EXPIRED:
            token = await self._tokens.refresh_expired(token)
            payload = await self._request(token, audio)
        status = int(payload.get("status") or 0)
        if status == ALIYUN_TOO_MANY_REQUESTS:
            raise BusinessError(
                "VOICE_CONCURRENCY_EXCEEDED",
                "阿里云语音识别并发已满，请稍后重试",
                status_code=429,
                details={"retry_after_seconds": self._settings.voice_lease_seconds},
            )
        if status != ALIYUN_SUCCESS:
            raise BusinessError(
                "TRANSCRIPTION_FAILED",
                "语音转写服务暂时不可用",
                status_code=502,
                details={"provider_status": status or None},
            )
        transcript = str(payload.get("result") or "").strip()
        if not transcript:
            raise BusinessError("TRANSCRIPTION_EMPTY", "语音转写没有返回文字", status_code=502)
        return transcript

    async def _request(self, token: str, audio: ValidatedWav) -> dict:
        app_key = self._settings.aliyun_nls_app_key
        if app_key is None or not app_key.get_secret_value().strip():
            raise BusinessError("TRANSCRIPTION_NOT_CONFIGURED", "阿里云语音 AppKey 尚未配置", status_code=503)
        params: dict[str, str | int] = {
            "appkey": app_key.get_secret_value(),
            "format": "wav",
            "sample_rate": 16_000,
            "enable_punctuation_prediction": "true",
            "enable_inverse_text_normalization": "true",
            "enable_voice_detection": "true",
        }
        if self._settings.aliyun_nls_vocabulary_id:
            params["vocabulary_id"] = self._settings.aliyun_nls_vocabulary_id
        try:
            timeout = httpx.Timeout(30, connect=5)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self._settings.aliyun_nls_endpoint,
                    params=params,
                    headers={"X-NLS-Token": token, "Content-Type": "application/octet-stream"},
                    content=audio.content,
                )
            if response.status_code == 429:
                raise BusinessError(
                    "VOICE_CONCURRENCY_EXCEEDED",
                    "阿里云语音识别并发已满，请稍后重试",
                    status_code=429,
                    details={"retry_after_seconds": self._settings.voice_lease_seconds},
                )
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    "upstream server error",
                    request=response.request,
                    response=response,
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid response")
            return payload
        except BusinessError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise BusinessError("TRANSCRIPTION_FAILED", "语音转写服务暂时不可用", status_code=502) from None


class OpenAITranscriber:
    async def transcribe(self, audio: ValidatedWav) -> str:
        return await openai_transcribe_audio("question.wav", "audio/wav", audio.content)


@lru_cache(maxsize=1)
def get_speech_transcriber() -> SpeechTranscriber:
    settings = get_settings()
    if settings.voice_stt_provider == "openai":
        return OpenAITranscriber()
    return AliyunNlsTranscriber(settings)


def reset_speech_caches() -> None:
    get_speech_transcriber.cache_clear()
