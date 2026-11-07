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
    "inject_habits": True,
    "pressure_scale": 25.0,
    "retention_days": 90,
    "base_model": "Qwen/Qwen3-0.6B",
    "tiny_model": "HuggingFaceTB/SmolLM2-135M-Instruct",
    "trainer_python": "",
    "cycles": 4,
    "night_max_minutes": 60,
    "probe_pass_threshold": 0.9,
    "brain_port": 8765,
    "routing": "auto",
    "shadow_agree_turns": 5,
    "confidence_threshold": -0.35,
    "brain_timeout_s": 20,
    "brain_max_context": 3072,
    "brain_endpoint": "",
    "auto_start_brain_server": True,
    "teacher": "auto",
    "dream_variants": 4,
    "teacher_budget_tokens": 60000,
    "teacher_usd_per_mtok": 3.0,
    "night_max_usd": 0.5,
    "sandbox": True,
    "desktop_reply_badges": True,
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
