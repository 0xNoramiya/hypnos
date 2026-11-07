"""``hermes hypnos ...`` subcommands (argparse)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any

from . import __version__, hermes


def setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="hypnos_cmd")
    sub.add_parser("status", help="Sleep pressure, signals and the current brain")
    sub.add_parser("doctor", help="Check Hypnos's environment and print fixes")
    habits = sub.add_parser("habits", help="Show the habit ledger")
    habits.add_argument("action", nargs="?", choices=["refresh"])
    sleep = sub.add_parser("sleep", help="Sleep now: replay the day, dream, consolidate, take the exam")
    sleep.add_argument("--tiny", action="store_true", help="Use the tiny model and a short night (for a quick try)")
    sleep.add_argument("--cycles", type=int, help="Sleep cycles (default from settings)")
    sleep.add_argument("--max-minutes", type=float, help="Hard time cap for the night")
    sleep.add_argument("--if-idle", type=float, metavar="MINUTES", help="Only sleep if Hermes has been idle this long")
    sleep.add_argument("--background", action="store_true", help="Start the night in the background and return")
    sleep.add_argument("--quiet", action="store_true", help="Print only the morning report")
    sleep.add_argument("--trigger", default="cli", help=argparse.SUPPRESS)
    report = sub.add_parser("report", help="Show a morning report")
    report.add_argument("night", nargs="?", type=int)
    gram = sub.add_parser("hypnogram", help="Show a night's sleep stages")
    gram.add_argument("night", nargs="?", type=int)
    brain = sub.add_parser("brain", help="Brain versions and lineage")
    brain.add_argument("action", nargs="?", default="list", choices=["list", "rollback"])
    brain.add_argument("version", nargs="?", type=int)
    serve = sub.add_parser("serve", help="Start, stop or inspect the local brain server")
    serve.add_argument("action", nargs="?", default="status", choices=["start", "stop", "status"])
    mark = sub.add_parser("mark", help="Mark the last reply good or bad")
    mark.add_argument("verdict", choices=["good", "bad"])
    mark.add_argument("note", nargs="*")


def run(args: Any, commands: Any, llm: Any = None) -> int:
    cmd = getattr(args, "hypnos_cmd", None) or "status"
    if cmd == "status":
        print(commands.status())
        return 0
    if cmd == "doctor":
        ok = doctor()
        return 0 if ok else 1
    if cmd == "habits":
        print(commands.hypnos("habits " + (args.action or "")))
        return 0
    if cmd == "sleep":
        from . import sleepcmd
        if args.background:
            extra = [a for a, on in (("--tiny", args.tiny), ("--quiet", True)) if on]
            if args.cycles:
                extra += ["--cycles", str(args.cycles)]
            if args.max_minutes:
                extra += ["--max-minutes", str(args.max_minutes)]
            pid = sleepcmd.spawn_detached(extra + ["--trigger", "cli"], hermes.data_dir())
            print(f"Sleeping in the background (pid {pid}). `hermes hypnos hypnogram` shows progress.")
            return 0
        return sleepcmd.run_sleep(db_path=hermes.db_path(), data_dir=hermes.data_dir(), memories_dir=hermes.memories_dir(),
                                  settings=commands.settings(), tiny=args.tiny, cycles=args.cycles,
                                  max_minutes=args.max_minutes, trigger=args.trigger, if_idle=args.if_idle,
                                  broadcast=hermes.broadcast, quiet=args.quiet,
                                  llm=llm if commands.settings().get("teacher", "auto") != "scripted" else None)
    if cmd == "report":
        from .sleepcmd import report_text
        print(report_text(hermes.db_path(), args.night))
        return 0
    if cmd == "hypnogram":
        from .sleepcmd import hypnogram_text
        print(hypnogram_text(hermes.db_path(), args.night))
        return 0
    if cmd == "brain":
        from . import sleepcmd
        if args.action == "rollback":
            if args.version is None:
                print("Usage: hermes hypnos brain rollback VERSION (0 for no brain)")
                return 2
            message, target = sleepcmd.rollback(hermes.db_path(), args.version)
            sleepcmd.sync_server(hermes.data_dir(), target, commands.settings())
            print(message)
            return 0
        print(sleepcmd.brain_list_text(hermes.db_path()))
        return 0
    if cmd == "serve":
        return serve(args.action, commands.settings())
    if cmd == "mark":
        print(commands.hypnos(f"{args.verdict} {' '.join(args.note or [])}"))
        commands.flush()
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


def serve(action: str, settings: dict) -> int:
    from . import brainserver
    from . import db as dbmod
    data = hermes.data_dir()
    if action == "stop":
        print("Brain server stopped." if brainserver.stop(data) else "Brain server was not running.")
        return 0
    if action == "status":
        st = brainserver.status(data)
        if not st.get("running"):
            print("Brain server is not running.")
        else:
            print(f"Brain server pid {st['pid']} on 127.0.0.1:{st['port']}, brain v{st.get('version')}, "
                  f"{st.get('base_model')}, healthy={bool(st.get('health'))}")
        return 0
    conn = dbmod.open_db(hermes.db_path())
    brain = conn.execute("SELECT * FROM brains WHERE status = 'promoted' ORDER BY version DESC LIMIT 1").fetchone()
    conn.close()
    if brain is None:
        print("No promoted brain yet. Run `hermes hypnos sleep` first.")
        return 1
    print(f"Starting the brain server for v{brain['version']} (loading {brain['base_model']})...")
    st = brainserver.start(data, base_model=brain["base_model"], adapter=brain["adapter_path"], version=brain["version"],
                           trainer_python=str(settings.get("trainer_python") or ""),
                           port=int(settings.get("brain_port") or brainserver.DEFAULT_PORT))
    print(f"Brain server pid {st.get('pid')} on 127.0.0.1:{st.get('port')}, healthy={bool(st.get('health'))}")
    return 0


def _check(label: str, ok: bool, detail: str = "", fix: str = "") -> bool:
    mark = "ok  " if ok else "warn"
    print(f"  [{mark}] {label}" + (f": {detail}" if detail else ""))
    if not ok and fix:
        for line in fix.splitlines():
            print(f"         {line}")
    return ok


def doctor() -> bool:
    print(f"Hypnos {__version__} doctor")
    all_ok = True
    data = hermes.data_dir()
    all_ok &= _check("data directory", data.exists(), str(data))
    from . import db as dbmod
    try:
        conn = dbmod.open_db(hermes.db_path())
        version = dbmod.schema_version(conn)
        conn.close()
        all_ok &= _check("database", version == dbmod.LATEST, f"schema v{version}")
    except Exception as exc:
        all_ok &= _check("database", False, str(exc))
    from .workerproc import install_hint, resolve_trainer_python
    python = resolve_trainer_python("", data)
    trainer_ok = False
    detail = "not found"
    if python:
        try:
            probe = subprocess.run([str(python), "-c", "import torch, transformers, peft; print(torch.__version__, "
                                    "torch.cuda.is_available())"], capture_output=True, text=True, timeout=120)
            trainer_ok = probe.returncode == 0
            detail = f"{python} (torch {probe.stdout.strip()})" if trainer_ok else f"{python}: {probe.stderr.strip()[-200:]}"
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)
    all_ok &= _check("trainer interpreter", trainer_ok, detail, install_hint(data))
    from . import sandbox
    usable, reason = sandbox.available()
    _check("docker sandbox for REM dreams", usable, "ready" if usable else reason,
           f"Optional. Without it, REM runs in lucid mode (pattern verifiers only, no execution).\n"
           f"To enable: {sandbox.PULL_HINT}")
    return all_ok
