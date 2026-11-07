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
    brain.add_argument("action", nargs="?", default="list", choices=["list", "rollback", "diff", "export"])
    brain.add_argument("versions", nargs="*", type=int)
    brain.add_argument("--to", help="Export folder (default plugin-data/hypnos/exports)")
    brain.add_argument("--push-to-hub", metavar="REPO", help="After exporting, run `hf upload REPO FOLDER` with your own hf login")
    lethe = sub.add_parser("lethe", help="Forget a session, a habit, or matching turns, exactly")
    lethe.add_argument("kind", choices=["session", "habit", "search", "run"])
    lethe.add_argument("target", nargs="*")
    lethe.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    serve = sub.add_parser("serve", help="Start, stop or inspect the local brain server")
    serve.add_argument("action", nargs="?", default="status", choices=["start", "stop", "status"])
    demo = sub.add_parser("demo", help="Synthetic demo: simulated days and real nights")
    demo.add_argument("action", choices=["timelapse", "reset"])
    demo.add_argument("--days", type=int, default=5)
    demo.add_argument("--tiny", action="store_true", help="Use the tiny model so each night takes minutes")
    demo.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
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
        if args.action == "diff":
            if len(args.versions) != 2:
                print("Usage: hermes hypnos brain diff A B")
                return 2
            from . import db as dbmod
            from . import lineage
            conn = dbmod.open_db(hermes.db_path())
            print(lineage.diff(conn, hermes.data_dir(), args.versions[0], args.versions[1]))
            conn.close()
            return 0
        if args.action == "export":
            return export_cmd(args)
        if args.action == "rollback":
            if len(args.versions) != 1:
                print("Usage: hermes hypnos brain rollback VERSION (0 for no brain)")
                return 2
            message, target = sleepcmd.rollback(hermes.db_path(), args.versions[0])
            sleepcmd.sync_server(hermes.data_dir(), target, commands.settings())
            print(message)
            return 0
        print(sleepcmd.brain_list_text(hermes.db_path()))
        return 0
    if cmd == "demo":
        return demo_cmd(args, commands)
    if cmd == "lethe":
        return lethe_cmd(args, commands)
    if cmd == "serve":
        return serve(args.action, commands.settings())
    if cmd == "mark":
        print(commands.hypnos(f"{args.verdict} {' '.join(args.note or [])}"))
        commands.flush()
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


def demo_cmd(args: Any, commands: Any) -> int:
    import shutil as _shutil

    from . import db as dbmod
    from . import demo, sleepcmd
    from .dreams import Dreamer, ScriptedTeacher
    from .night import Night
    db_path, data = hermes.db_path(), hermes.data_dir()
    if demo.has_real_data(db_path) and not args.force:
        print("This profile has real captured turns. Run the demo in its own profile so it never mixes with them:\n"
              "  hermes profile create hypnos-demo\n  hermes -p hypnos-demo plugins enable hypnos\n"
              "  hermes -p hypnos-demo hypnos demo timelapse --days 5 --tiny")
        return 1
    if args.action == "reset":
        from . import brainserver
        brainserver.stop(data)
        for name in ("data.db", "data.db-wal", "data.db-shm"):
            (data / name).unlink(missing_ok=True)
        for sub in ("brains", "nights", "exports"):
            _shutil.rmtree(data / sub, ignore_errors=True)
        print("Demo data removed.")
        return 0
    settings = commands.settings()
    print(f"Hypnos timelapse: {args.days} SYNTHETIC days with a scripted developer persona"
          f"{' (tiny model)' if args.tiny else ''}. Nights are real: dreams, training, exam.")

    def night(day: int) -> dict:
        cfg = sleepcmd.night_config(settings, tiny=args.tiny, trigger="demo")
        dreamer = Dreamer(ScriptedTeacher(seed=day), k=3, use_sandbox=bool(settings.get("sandbox", True)))
        try:
            return Night(db_path, data, cfg, dreamer=dreamer, memories_dir=None).run()
        except Exception as exc:
            print(f"  the night failed: {exc}")
            return {}

    def start(report: dict) -> None:
        conn = dbmod.open_db(db_path)
        brain = conn.execute("SELECT * FROM brains WHERE version = ?", (report["version"],)).fetchone()
        conn.close()
        sleepcmd.sync_server(data, dict(brain) if brain else None, {**settings, "routing": "auto"},
                             allow_start=True, out=print)

    summary = demo.Timelapse(db_path=db_path, data_dir=data, settings={}, days=args.days, tiny=args.tiny,
                             night_factory=night, start_server=start).run()
    from . import webapi
    days = webapi.metrics(db_path)["days"]
    print("\nDay  turns  prompt tokens/turn  brain share  success")
    for i, d in enumerate(days, 1):
        print(f"{i:>3}  {d['turns']:>5}  {d['avg_prompt_tokens']:>18,}  {d['brain_share']:>10.0%}  {d['success']:>7.0%}")
    print("\nSYNTHETIC demo data. Open Hermes Desktop > Hypnos for the hypnograms and morning reports.")
    return 0 if summary else 1


def export_cmd(args: Any) -> int:
    from pathlib import Path

    from . import db as dbmod
    from . import lineage
    conn = dbmod.open_db(hermes.db_path())
    try:
        version = args.versions[0] if args.versions else conn.execute(
            "SELECT MAX(version) FROM brains WHERE status = 'promoted'").fetchone()[0]
        if version is None:
            print("No brain to export yet.")
            return 1
        out = lineage.export(conn, version, Path(args.to) if args.to else hermes.data_dir() / "exports")
    finally:
        conn.close()
    print(f"Exported brain v{version} to {out}: adapter, model card and manifest hash. No captured data is included.")
    if args.push_to_hub:
        hf = shutil_which("hf")
        if not hf:
            print("The hf CLI is not installed; upload the folder yourself.")
            return 1
        print(f"Uploading with your own hf login: hf upload {args.push_to_hub} {out}")
        return subprocess.run([hf, "upload", args.push_to_hub, str(out)]).returncode
    return 0


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def lethe_cmd(args: Any, commands: Any) -> int:
    import sys as _sys

    from . import db as dbmod
    from . import lineage, sleepcmd
    db_path = hermes.db_path()
    if args.kind == "run":
        conn = dbmod.open_db(db_path)
        data = dbmod.get_meta(conn, "lethe_running")
        dbmod.set_meta(conn, "lethe_running", None)
        conn.close()
        if not data:
            print("No Lethe retraining is pending.")
            return 0
        data.pop("created", None)
        return sleepcmd.lethe_run(db_path=db_path, data_dir=hermes.data_dir(), memories_dir=hermes.memories_dir(),
                                  settings=commands.settings(), plan=lineage.LethePlan(**data))
    target = " ".join(args.target)
    if not target:
        print("Usage: hermes hypnos lethe session|habit|search TARGET [--yes]")
        return 2
    conn = dbmod.open_db(db_path)
    try:
        plan = lineage.plan(conn, args.kind, target)
    except ValueError as exc:
        print(exc)
        return 1
    finally:
        conn.close()
    if not (plan.episodes or plan.pairs or plan.habits or plan.dreams):
        print(f"Nothing matches {args.kind} {target}.")
        return 0
    print(plan.summary())
    if not args.yes:
        if not _sys.stdin.isatty():
            print("Refusing without confirmation: rerun with --yes.")
            return 1
        from hermes_cli.setup import prompt_yes_no
        if not prompt_yes_no("Forget this and retrain?", default=False):
            print("Cancelled. Nothing was changed.")
            return 0
    needs, message = sleepcmd.lethe_apply(db_path, plan)
    print(message)
    if not needs:
        print("No brain needs retraining.")
        return 0
    return sleepcmd.lethe_run(db_path=db_path, data_dir=hermes.data_dir(), memories_dir=hermes.memories_dir(),
                              settings=commands.settings(), plan=plan)


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
