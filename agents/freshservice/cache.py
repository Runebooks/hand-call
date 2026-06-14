"""Process-local TTL cache for Freshservice agent tool results (no Redis required)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Optional


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def cache_enabled() -> bool:
    flag = os.environ.get("FRESHSERVICE_CACHE_ENABLED", "true").strip().lower()
    return flag in ("1", "true", "yes", "on")


class TTLCache:
    def __init__(self, default_ttl: float = 300.0, max_entries: int = 512):
        self._default_ttl = default_ttl
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if now >= expires:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expires = time.time() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            if len(self._data) >= self._max:
                self._evict_oldest()
            self._data[key] = (expires, value)

    def _evict_oldest(self) -> None:
        if not self._data:
            return
        oldest_key = min(self._data, key=lambda k: self._data[k][0])
        del self._data[oldest_key]


def tool_cache_key(tool: str, args: dict[str, Any]) -> str:
    payload = json.dumps(args or {}, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"fs:{tool}:{digest}"


# Shared caches — TTLs overridable via env
TOOL_CACHE = TTLCache(default_ttl=_env_float("FRESHSERVICE_TOOL_CACHE_TTL", 300.0))
PIR_CACHE = TTLCache(default_ttl=_env_float("FRESHSERVICE_PIR_CACHE_TTL", 3600.0))
FRESHSTATUS_CACHE = TTLCache(default_ttl=_env_float("FRESHSERVICE_FRESHSTATUS_CACHE_TTL", 90.0))
