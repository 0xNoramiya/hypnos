"""Settings with defaults. ``ctx.get_config`` does not apply ``config_schema`` defaults, so we do.

``DEFAULTS`` must match the defaults in ``plugin.yaml``'s ``config_schema`` (a test checks this).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "exclude_platforms": ["cron"],
    "capture_subagents": False,
    "pressure_scale": 25.0,
    "retention_days": 90,
}


class Settings:
    """Cached view over ``ctx.get_config``. Hooks call this on every event, so reads are cached briefly.

    The cache is keyed by profile home, because one gateway can serve several profiles.
    """

    def __init__(self, getter: Callable[[str, Any], Any], home: Callable[[], str] = lambda: "", ttl: float = 3.0):
        self._get = getter
        self._home = home
        self._ttl = ttl
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def __call__(self) -> dict:
        key = self._home()
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        values = {}
        for name, default in DEFAULTS.items():
            try:
                value = self._get(name, default)
            except Exception:
                value = default
            values[name] = default if value is None else value
        with self._lock:
            self._cache[key] = (time.monotonic(), values)
        return values

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
