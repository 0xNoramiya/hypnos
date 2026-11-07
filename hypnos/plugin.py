"""Hermes entry point. Wires hooks, commands and the CLI. Must stay fast: no heavy imports here."""

from __future__ import annotations

import atexit
import logging
from typing import Any

from . import hermes
from .capture import Capture, Writer
from .commands import Commands
from .config import Settings

log = logging.getLogger("hypnos")

_WRITER: Writer | None = None


def _writer() -> Writer:
    global _WRITER
    if _WRITER is None:
        _WRITER = Writer()
        atexit.register(_WRITER.flush, 2.0)
    return _WRITER


def register(ctx: Any) -> None:
    settings = Settings(ctx.get_config, home=hermes.hermes_home)
    writer = _writer()
    capture = Capture(db_path=hermes.db_path, settings=settings, writer=writer)
    for name, callback in capture.hooks().items():
        ctx.register_hook(name, callback)

    commands = Commands(db_path=hermes.db_path, settings=settings, set_config=ctx.set_config,
                        mark=capture.mark, flush=lambda: writer.flush(1.0), memories_dir=hermes.memories_dir,
                        on_habits_changed=capture.injector.invalidate)

    def hypnos_command(raw_args: str = "", session_id: str = "", **_: Any) -> str:
        try:
            return commands.hypnos(raw_args, session_id=session_id)
        except Exception as exc:
            log.warning("hypnos: /hypnos failed", exc_info=True)
            return f"Hypnos hit an error: {exc}"

    ctx.register_command("hypnos", handler=hypnos_command,
                         description="Hypnos status, on/off, and good/bad marks",
                         args_hint="[status|habits|on|off|good|bad|help]")

    from . import cli
    ctx.register_cli_command("hypnos", help="Hypnos: Hermes learns in its sleep",
                             setup_fn=cli.setup, handler_fn=lambda args: cli.run(args, commands),
                             description="Inspect and run Hypnos (status, doctor)")
