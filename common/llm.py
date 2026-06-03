"""
OpenAI LLM client for natural-language → structured intent parsing.

Configure via environment (never commit secrets):
  OPENAI_API_KEY    — API key (sk-...)
  OPENAI_MODEL      — default: gpt-5.5
  OPENAI_REASONING_EFFORT — optional: none, low, medium, high (default: none)
  OPENAI_BASE_URL   — default: https://api.openai.com/v1
  LLM_ENABLED       — default: true when API key is set
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIClient:
    """Minimal OpenAI Chat Completions client with JSON response format."""

    provider = "openai"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")

    def enabled(self) -> bool:
        flag = os.environ.get("LLM_ENABLED", "").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return False
        if flag in ("1", "true", "yes", "on"):
            return bool(self.api_key)
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

    def complete_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        """Call OpenAI and parse a JSON object from the response."""
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        reasoning = os.environ.get("OPENAI_REASONING_EFFORT", "none").strip()
        if reasoning and self.model.startswith("gpt-5"):
            payload["reasoning_effort"] = reasoning

        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"OpenAI API error {response.status_code}: {response.text[:500]}"
                )
            data = response.json()

        text = _extract_openai_text(data)
        return _parse_json_object(text)


# Alias used by the kubernetes agent
LLMClient = OpenAIClient


def _extract_openai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI returned no choices")
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return json.loads(text)
