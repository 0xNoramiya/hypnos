"""The only module that imports Hermes. Every import is lazy and limited to the public plugin API.

Public modules used: ``plugins.plugin_storage``, ``hermes_constants``, ``agent.secret_scope``.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_ID = "hypnos"


def hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")


_DATA_DIRS: dict[str, str] = {}


def data_dir() -> Path:
    """``<HERMES_HOME>/plugin-data/hypnos``. Resolved per profile, cached per profile home."""
    home = hermes_home()
    cached = _DATA_DIRS.get(home)
    if cached:
        return Path(cached)
    try:
        from plugins.plugin_storage import plugin_data_dir
        path = Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        path = Path(home) / "plugin-data" / PLUGIN_ID
        path.mkdir(parents=True, exist_ok=True)
    _DATA_DIRS[home] = str(path)
    return path


def db_path() -> str:
    return str(data_dir() / "data.db")


def memories_dir() -> Path:
    """Where Hermes keeps MEMORY.md and USER.md. Hypnos only ever reads these files."""
    return Path(hermes_home()) / "memories"


def secret(name: str) -> str:
    """Read one of Hypnos's own declared secrets through Hermes. Never reads files."""
    try:
        from agent.secret_scope import get_secret
        return get_secret(name) or ""
    except Exception:
        return ""


def broadcast(event: str, payload: dict) -> None:
    """Push a live event to the Desktop. Delivered only from the gateway process; dropped elsewhere."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_ID, event, payload)
    except Exception:
        pass
