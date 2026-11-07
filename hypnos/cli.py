"""``hermes hypnos ...`` subcommands (argparse)."""

from __future__ import annotations

import argparse
import shutil
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
    mark = sub.add_parser("mark", help="Mark the last reply good or bad")
    mark.add_argument("verdict", choices=["good", "bad"])
    mark.add_argument("note", nargs="*")


def run(args: Any, commands: Any) -> int:
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
    if cmd == "mark":
        print(commands.hypnos(f"{args.verdict} {' '.join(args.note or [])}"))
        commands.flush()
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


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
    docker = shutil.which("docker")
    docker_ok = False
    if docker:
        try:
            docker_ok = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                                       text=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            docker_ok = False
    _check("docker sandbox for REM dreams", docker_ok, "available" if docker_ok else "not available",
           "Without Docker, REM runs in lucid mode: pattern verifiers only, no execution.")
    return all_ok
