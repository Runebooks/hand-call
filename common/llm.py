"""
OpenAI-compatible LLM client for natural-language → structured intent parsing.

Configure via environment (never commit secrets):
  OPENAI_API_KEY        — OpenAI sk-... OR Cloudverse / Freddy JWT (eyJ...)
  OPENAI_AUTH_MODE      — auto | openai | cloudverse (default: auto)
  OPENAI_MODEL          — default: gpt-5.5
  CLOUDVERSE_BASE_URL   — Cloudverse gateway, e.g. https://<host>/v1 (JWT only)
  OPENAI_BASE_URL       — overrides base URL (use CLOUDVERSE_BASE_URL for JWT)
  OPENAI_REASONING_EFFORT — optional: none, low, medium, high (default: none)
  LLM_ENABLED           — default: true when API key + compatible base URL are set
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
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

JWT_INCOMPATIBLE_HINT = (
    "OPENAI_API_KEY is a Cloudverse / Freddy JWT (eyJ...). It cannot call "
    "https://api.openai.com. Set CLOUDVERSE_BASE_URL (or OPENAI_BASE_URL) to your "
    "org's Cloudverse OpenAI-compatible gateway, e.g. from the Cloudverse playground "
    "or internal docs. Heuristic intent parsing remains enabled."
)


def is_jwt_token(api_key: str) -> bool:
    key = (api_key or "").strip()
    return key.count(".") == 2 and key.startswith("eyJ")


def is_public_openai_base(base_url: str) -> bool:
    base = (base_url or "").lower()
    return "api.openai.com" in base


def auth_mode() -> str:
    mode = (os.environ.get("OPENAI_AUTH_MODE") or "auto").strip().lower()
    if mode in ("openai", "cloudverse", "auto"):
        return mode
    return "auto"


def resolve_base_url(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    cloudverse = (os.environ.get("CLOUDVERSE_BASE_URL") or "").strip()
    openai_base = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    if cloudverse:
        return cloudverse.rstrip("/")
    if openai_base:
        return openai_base.rstrip("/")
    if auth_mode() == "cloudverse":
        return ""
    return DEFAULT_OPENAI_BASE_URL


def jwt_incompatible_base(api_key: str, base_url: str) -> bool:
    """JWT + public OpenAI always fails with 401 invalid issuer."""
    if os.environ.get("LLM_ALLOW_JWT", "").lower() in ("1", "true", "yes"):
        return False
    if not is_jwt_token(api_key):
        return False
    if not base_url:
        return auth_mode() != "cloudverse"
    return is_public_openai_base(base_url)


class OpenAIClient:
    """Minimal OpenAI Chat Completions client with JSON response format."""

    provider = "openai"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = (
            api_key
            or os.environ.get("OPENAI_API_KEY_SK", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        self.base_url = resolve_base_url(base_url)
        self._auth_mode = self._detect_auth_mode()
        # None = unknown (probe on first tool call), True/False once observed.
        self._supports_tools: Optional[bool] = None

    def _detect_auth_mode(self) -> str:
        mode = auth_mode()
        if mode != "auto":
            return mode
        if is_jwt_token(self.api_key):
            return "cloudverse"
        if self.api_key.startswith("sk-"):
            return "openai"
        return "openai"

    def enabled(self) -> bool:
        flag = os.environ.get("LLM_ENABLED", "").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return False
        if not self.api_key:
            return False
        if jwt_incompatible_base(self.api_key, self.base_url):
            return False
        if self._auth_mode == "cloudverse" and not self.base_url:
            return False
        if flag in ("1", "true", "yes", "on"):
            return True
        return True

    @property
    def disabled_reason(self) -> str:
        if not self.api_key:
            return "OPENAI_API_KEY not set"
        if self._auth_mode == "cloudverse" and not self.base_url:
            return "cloudverse_base_url_missing"
        if jwt_incompatible_base(self.api_key, self.base_url):
            return "jwt_incompatible_with_openai"
        flag = os.environ.get("LLM_ENABLED", "").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return "LLM_ENABLED=false"
        return ""

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
        """Call OpenAI-compatible API and parse a JSON object from the response."""
        if not self.enabled():
            reason = self.disabled_reason
            if reason == "jwt_incompatible_with_openai":
                raise RuntimeError(JWT_INCOMPATIBLE_HINT)
            if reason == "cloudverse_base_url_missing":
                raise RuntimeError(
                    "CLOUDVERSE_BASE_URL is not set. Add your org's Cloudverse "
                    "OpenAI-compatible base URL (…/v1) to .env.local."
                )
            raise RuntimeError("LLM is disabled (OPENAI_API_KEY / LLM_ENABLED / base URL)")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # Cloudverse gateway may not support OpenAI response_format yet.
        if self._auth_mode != "cloudverse":
            payload["response_format"] = {"type": "json_object"}
        reasoning = os.environ.get("OPENAI_REASONING_EFFORT", "none").strip()
        if reasoning and self.model.startswith("gpt-5"):
            payload["reasoning_effort"] = reasoning

        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"LLM API error {response.status_code}: {response.text[:500]}"
                )
            data = response.json()

        text = _extract_openai_text(data)
        return _parse_json_object(text)

    @property
    def supports_tools(self) -> Optional[bool]:
        """None until probed, then True/False based on observed gateway behavior."""
        return self._supports_tools

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: Optional[float] = None,
    ) -> dict[str, Any]:
        """Raw chat completion.

        Returns the assistant `message` dict (may include `tool_calls`). When
        `tools` are passed but the gateway rejects them, raises ToolsUnsupported
        so the caller can fall back to a JSON planner loop.
        """
        if not self.enabled():
            reason = self.disabled_reason
            if reason == "jwt_incompatible_with_openai":
                raise RuntimeError(JWT_INCOMPATIBLE_HINT)
            if reason == "cloudverse_base_url_missing":
                raise RuntimeError(
                    "CLOUDVERSE_BASE_URL is not set. Add your org's Cloudverse "
                    "OpenAI-compatible base URL (…/v1) to .env.local."
                )
            raise RuntimeError("LLM is disabled (OPENAI_API_KEY / LLM_ENABLED / base URL)")

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        reasoning = os.environ.get("OPENAI_REASONING_EFFORT", "none").strip()
        if reasoning and reasoning != "none" and self.model.startswith("gpt-5"):
            payload["reasoning_effort"] = reasoning

        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=90.0) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            if response.status_code >= 400:
                body = response.text[:500]
                if tools and _looks_like_tools_unsupported(response.status_code, body):
                    self._supports_tools = False
                    raise ToolsUnsupported(
                        f"Gateway rejected tools ({response.status_code}): {body}"
                    )
                raise RuntimeError(f"LLM API error {response.status_code}: {body}")
            data = response.json()

        if tools:
            self._supports_tools = True
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("LLM returned no choices")
        return choices[0].get("message") or {}


class ToolsUnsupported(RuntimeError):
    """Raised when the configured gateway does not support OpenAI tool calling."""


def _looks_like_tools_unsupported(status: int, body: str) -> bool:
    text = (body or "").lower()
    if status not in (400, 404, 422, 501):
        return False
    return any(
        token in text
        for token in (
            "tool",
            "function",
            "tool_choice",
            "not supported",
            "unsupported",
            "unrecognized",
            "unknown field",
            "invalid",
        )
    )


# Alias used by the kubernetes agent
LLMClient = OpenAIClient


def _extract_openai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
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
