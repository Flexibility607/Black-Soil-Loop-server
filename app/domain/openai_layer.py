from __future__ import annotations

from typing import Any

import httpx

from app.shared.config import get_settings
from app.shared.errors import BusinessError

INTENTS = {
    "CHANNEL",
    "ALERT",
    "OPERATIONS",
    "INVENTORY",
    "TRANSPORT",
    "CARPOOL",
    "WAREHOUSE",
    "PROCUREMENT",
    "FORECAST",
    "OVERVIEW",
}


def _response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"].strip()
    return ""


def classify_intent(question: str) -> str | None:
    settings = get_settings()
    if not settings.openai_api_key or not settings.openai_model:
        return None
    prompt = (
        "Classify the Chinese dashboard question into exactly one token from: "
        + ", ".join(sorted(INTENTS))
        + ". Return only the token. Do not answer the question, generate SQL, or infer data.\nQuestion: "
        + question
    )
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.openai_model, "input": prompt, "store": False},
            timeout=20,
        )
        response.raise_for_status()
        intent = _response_text(response.json()).strip().upper()
        return intent if intent in INTENTS else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None


async def transcribe_audio(filename: str, content_type: str, audio_bytes: bytes) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise BusinessError(
            "TRANSCRIPTION_NOT_CONFIGURED",
            "语音转写尚未配置；文字和预设问题仍可使用",
            status_code=503,
        )
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data={"model": settings.openai_transcription_model, "language": "zh"},
                files={"file": (filename, audio_bytes, content_type)},
            )
        response.raise_for_status()
        transcript = str(response.json().get("text") or "").strip()
        if not transcript:
            raise BusinessError("TRANSCRIPTION_EMPTY", "语音转写没有返回文字", status_code=502)
        return transcript
    except BusinessError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise BusinessError("TRANSCRIPTION_FAILED", "语音转写服务暂时不可用", status_code=502) from exc
