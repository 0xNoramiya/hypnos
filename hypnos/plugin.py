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

    from . import brainserver
    from .router import Router

    def brain_client(endpoint: str, body: dict, timeout: float) -> dict:
        return brainserver.http_json(endpoint.rstrip("/") + "/chat/completions", body, timeout=timeout)

    router = Router(db_path=hermes.db_path, settings=settings, writer=writer, brain_client=brain_client,
                    endpoint=lambda s: brainserver.endpoint(hermes.data_dir(), str(s.get("brain_endpoint") or "")))
    writer.on_brain_changed = router.invalidate
    ctx.register_middleware("llm_execution", router.on_llm_execution)

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
                         args_hint="[status|habits|report|on|off|good|bad|help]")

    from . import sleepcmd

    def sleep_command(raw_args: str = "", **_: Any) -> str:
        try:
            writer.flush(1.0)
            return sleepcmd.sleep_slash(raw_args, db_path=hermes.db_path(), data_dir=hermes.data_dir())
        except Exception as exc:
            log.warning("hypnos: /sleep failed", exc_info=True)
            return f"Hypnos hit an error: {exc}"

    def hypnogram_command(raw_args: str = "", **_: Any) -> str:
        try:
            arg = (raw_args or "").strip()
            return sleepcmd.hypnogram_text(hermes.db_path(), int(arg) if arg.isdigit() else None)
        except Exception as exc:
            return f"Hypnos hit an error: {exc}"

    def brain_command(raw_args: str = "", **_: Any) -> str:
        try:
            words = (raw_args or "").split()
            if words[:1] == ["rollback"] and len(words) == 2 and words[1].lstrip("v").isdigit():
                message, brain = sleepcmd.rollback(hermes.db_path(), int(words[1].lstrip("v")))
                sleepcmd.sync_server(hermes.data_dir(), brain, settings())
                return message
            return sleepcmd.brain_list_text(hermes.db_path())
        except Exception as exc:
            return f"Hypnos hit an error: {exc}"

    ctx.register_command("sleep", handler=sleep_command, description="Put Hypnos to sleep, check on it, or wake it",
                         args_hint="[now|status|cancel]")
    ctx.register_command("hypnogram", handler=hypnogram_command, description="Sleep stages of the latest night",
                         args_hint="[night]")
    ctx.register_command("brain", handler=brain_command, description="Hypnos brain versions and lineage",
                         args_hint="[list|rollback N]")

    if hasattr(ctx, "register_auxiliary_task"):
        try:
            ctx.register_auxiliary_task("hypnos_teacher", display_name="Hypnos teacher",
                                        description="Writes REM dream variants at night (budget-capped)",
                                        defaults={"timeout": 120})
        except Exception:
            log.debug("hypnos: could not register the teacher task", exc_info=True)

    from . import cli
    ctx.register_cli_command("hypnos", help="Hypnos: Hermes learns in its sleep",
                             setup_fn=cli.setup, handler_fn=lambda args: cli.run(args, commands, llm=ctx.llm),
                             description="Inspect and run Hypnos (status, doctor)")
