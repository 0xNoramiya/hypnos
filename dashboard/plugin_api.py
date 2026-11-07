"""Hypnos backend routes for the Desktop plugin, mounted at ``/api/plugins/hypnos/``.

The host imports this file on its own (no package), so the sibling ``hypnos`` core package is loaded by
path under a private module name. Every route resolves the active profile at call time.

    GET  /status            sleep pressure, brain, current night, last turn
    GET  /night?id=         hypnogram segments (latest night by default)
    GET  /nights            recent nights
    GET  /report?id=        a morning report (latest by default)
    GET  /turns/{turn_id}   who answered a turn and the tokens saved (reply badge)
    GET  /sessions          brain usage per session (sidebar row badges)
    GET  /habits            the habit ledger with consolidation state
    GET  /metrics           per-day prompt tokens per turn, brain share and success
    POST /sleep             start a night in the background
    POST /sleep/cancel      wake Hypnos up
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

_PACKAGE = "hermes_dashboard_plugin_hypnos_core"
_PACKAGE_PATH = Path(__file__).resolve().parents[1] / "hypnos"


def _core(module: str):
    if _PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PACKAGE, _PACKAGE_PATH / "__init__.py",
                                                      submodule_search_locations=[str(_PACKAGE_PATH)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE] = package
        try:
            spec.loader.exec_module(package)
        except Exception:
            sys.modules.pop(_PACKAGE, None)
            raise
    return importlib.import_module(f".{module}", _PACKAGE)


def _settings() -> dict:
    defaults = dict(_core("config").DEFAULTS)
    with contextlib.suppress(Exception):  # an unreadable config leaves the defaults in place
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        values = (((cfg.get("plugins") or {}).get("entries") or {}).get("hypnos") or {}).get("settings") or {}
        defaults.update({k: v for k, v in values.items() if v is not None})
    return defaults


def _paths() -> tuple[str, Path]:
    hermes = _core("hermes")
    return hermes.db_path(), hermes.data_dir()


@router.get("/status")
def get_status() -> dict:
    return _core("webapi").status(_paths()[0], _settings())


@router.get("/night")
def get_night(id: int | None = None) -> dict:  # `id` is the query parameter's name
    return _core("webapi").night(_paths()[0], id)


@router.get("/nights")
def get_nights() -> list:
    return _core("webapi").nights(_paths()[0])


@router.get("/report")
def get_report(id: int | None = None) -> dict:  # `id` is the query parameter's name
    return _core("webapi").report(_paths()[0], id)


@router.get("/turns/{turn_id:path}")
def get_turn(turn_id: str) -> dict:
    if len(turn_id) > 300:
        raise HTTPException(status_code=400, detail="turn id too long")
    return _core("webapi").turn(_paths()[0], turn_id)


@router.get("/sessions")
def get_sessions() -> dict:
    return _core("webapi").sessions(_paths()[0])


@router.get("/habits")
def get_habits() -> list:
    return _core("webapi").habits(_paths()[0])


@router.get("/metrics")
def get_metrics() -> dict:
    return _core("webapi").metrics(_paths()[0])


@router.post("/sleep")
def post_sleep(tiny: bool = False) -> dict:
    db_path, data_dir = _paths()
    return _core("webapi").start_sleep(db_path, data_dir, tiny=tiny)


@router.post("/sleep/cancel")
def post_cancel() -> dict:
    return _core("webapi").cancel_sleep(_paths()[0])
