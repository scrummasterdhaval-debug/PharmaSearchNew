from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import requests

from core.logging_config import get_logger


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
AI_MODEL = os.getenv("PHARMASEARCH_AI_MODEL", "gpt-5-mini")
AI_TIMEOUT_SECONDS = int(os.getenv("PHARMASEARCH_AI_TIMEOUT_SECONDS", "30"))
AI_MAX_TEXT_CHARS = int(os.getenv("PHARMASEARCH_AI_MAX_TEXT_CHARS", "12000"))
logger = get_logger(__name__)

AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "strength": {"type": "string"},
        "dosage_form": {"type": "string"},
        "route": {"type": "string"},
        "pack_size": {"type": "string"},
        "therapeutic_category": {"type": "string"},
        "ma_holder": {"type": "string"},
        "manufacturer_name": {"type": "string"},
        "manufacturer_country": {"type": "string"},
        "registration_number": {"type": "string"},
        "registration_date": {"type": "string"},
        "expiry_date": {"type": "string"},
        "atc_code": {"type": "string"},
        "language_detected": {"type": "string"},
        "confidence": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": [
        "strength",
        "dosage_form",
        "route",
        "pack_size",
        "therapeutic_category",
        "ma_holder",
        "manufacturer_name",
        "manufacturer_country",
        "registration_number",
        "registration_date",
        "expiry_date",
        "atc_code",
        "language_detected",
        "confidence",
        "evidence",
    ],
}


def ai_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY")) and os.getenv("PHARMASEARCH_AI_ENABLED", "true").lower() not in {
        "0",
        "false",
        "no",
    }


def ai_status() -> dict[str, Any]:
    return {
        "enabled": ai_enabled(),
        "provider": "OpenAI Responses API",
        "model": AI_MODEL,
        "api_key_configured": bool(os.getenv("OPENAI_API_KEY")),
    }


def _response_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _clean_json_object(text: str) -> dict[str, str]:
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value or "").strip() for key, value in parsed.items()}


@lru_cache(maxsize=256)
def _extract_from_text_cached(cache_key: str, document_text: str) -> tuple[tuple[str, str], ...]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return tuple()

    prompt = (
        "Extract pharmaceutical regulatory product fields from the supplied official document text. "
        "Return English values only. If a field is not explicitly present, return an empty string. "
        "Do not guess. Manufacturer means the manufacturing/batch release site, not the MA holder, "
        "unless the document explicitly says they are the same. Keep evidence concise."
    )
    body = {
        "model": AI_MODEL,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": prompt}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Cache key: {cache_key}\n\nDocument text:\n{document_text[:AI_MAX_TEXT_CHARS]}",
                    }
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "pharma_regulatory_extraction",
                "schema": AI_SCHEMA,
                "strict": True,
            }
        },
    }
    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=AI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        parsed = _clean_json_object(_response_text(response.json()))
        return tuple(sorted(parsed.items()))
    except Exception as exc:
        logger.warning("AI extraction failed for %s: %s", cache_key, exc)
        return tuple()


def ai_extract_regulatory_fields(row: dict[str, Any], document_text: str) -> dict[str, str]:
    if not ai_enabled() or not document_text.strip():
        return {}
    cache_key = "|".join(
        str(row.get(field, "")).strip()
        for field in ["source", "country", "product", "registration_number", "smpc_url", "pil_url", "url"]
    )
    return dict(_extract_from_text_cached(cache_key, document_text))
