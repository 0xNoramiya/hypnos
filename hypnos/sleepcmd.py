"""Sleep commands: run a night (CLI), start one in the background (slash), and read its results."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import db as dbmod
from .hypnogram import night_payload, render_text
from .night import Night, NightConfig, NightError, request_cancel
from .report import render_report


def night_config(settings: dict, *, tiny: bool = False, cycles: int | None = None, max_minutes: float | None = None,
                 trigger: str = "manual") -> NightConfig:
    return NightConfig(
        base_model=str(settings.get("base_model") or NightConfig.base_model),
        tiny_model=str(settings.get("tiny_model") or NightConfig.tiny_model),
        tiny=tiny,
        cycles=int(cycles or settings.get("cycles") or 4),
        max_minutes=float(max_minutes or settings.get("night_max_minutes") or 60),
        trainer_python=str(settings.get("trainer_python") or ""),
        consolidation_threshold=float(settings.get("probe_pass_threshold") or 0.9),
        trigger=trigger,
        **({"max_steps": 60, "max_heldout": 20, "probes_per_habit": 6, "epochs_sft": 4.0, "epochs_dpo": 4.0}
           if tiny else {}))


def run_sleep(*, db_path: str, data_dir: Path, memories_dir: Path | None, settings: dict, tiny: bool = False,
              cycles: int | None = None, max_minutes: float | None = None, trigger: str = "manual",
              if_idle: float | None = None, broadcast: Callable[[str, dict], None] | None = None,
              out: Callable[[str], None] = print, quiet: bool = False) -> int:
    conn = dbmod.open_db(db_path)
    if if_idle:
        last = dbmod.get_meta(conn, "last_activity") or 0
        idle_min = (time.time() - float(last)) / 60
        if last and idle_min < if_idle:
            out(f"Hermes was active {idle_min:.0f} min ago. Skipping sleep until it has been idle for {if_idle:.0f} min.")
            return 0
    conn.close()
    cfg = night_config(settings, tiny=tiny, cycles=cycles, max_minutes=max_minutes, trigger=trigger)
    log = (lambda s: None) if quiet else out
    night = Night(db_path, data_dir, cfg, memories_dir=memories_dir, broadcast=broadcast, log=log)
    try:
        report = night.run()
    except NightError as exc:
        out(f"Hypnos could not sleep: {exc}")
        return 1
    out("")
    out(render_report(report))
    return 0


def hermes_root() -> Path | None:
    spec = importlib.util.find_spec("hermes_cli")
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve().parent.parent


def spawn_detached(args: list[str], data_dir: Path) -> int:
    """Start ``hermes hypnos sleep ...`` in its own session so slash commands return immediately."""
    root = hermes_root()
    log_path = data_dir / "nights" / "last-run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} hypnos sleep {' '.join(args)}\n")
    log.flush()
    proc = subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "hypnos", "sleep", *args],
                            cwd=str(root) if root else None, env=os.environ.copy(), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def running_night(conn):
    return conn.execute("SELECT * FROM nights WHERE status IN ('running', 'cancelling') ORDER BY id DESC LIMIT 1").fetchone()


def sleep_slash(raw: str, *, db_path: str, data_dir: Path, spawn: Callable[[list[str], Path], int] = spawn_detached) -> str:
    words = (raw or "").split()
    sub = words[0].lower() if words else "status"
    conn = dbmod.open_db(db_path)
    try:
        current = running_night(conn)
        if sub == "now":
            if current:
                return f"Already asleep: night #{current['id']}. /hypnogram shows where it is."
            args = ["--trigger", "slash"] + (["--tiny"] if "--tiny" in words or "tiny" in words else [])
            pid = spawn(args, data_dir)
            return (f"Lights out. Hypnos is sleeping in the background (pid {pid}). Watch it with /hypnogram; "
                    f"the morning report arrives with /hypnos report.")
        if sub == "cancel":
            return "Waking Hypnos up; tonight's training is discarded." if request_cancel(conn) else "Hypnos is not asleep."
        if sub == "status":
            if current:
                return render_text(night_payload(conn, current["id"]))
            last = conn.execute("SELECT * FROM nights ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                return "Hypnos has never slept. Run /sleep now."
            return f"Awake. Last night #{last['id']} ended {last['status']}. /hypnos report for details."
        return "Usage: /sleep [now|status|cancel]"
    finally:
        conn.close()


def hypnogram_text(db_path: str, night_id: int | None = None) -> str:
    conn = dbmod.open_db(db_path)
    try:
        return render_text(night_payload(conn, night_id))
    finally:
        conn.close()


def report_text(db_path: str, night_id: int | None = None) -> str:
    conn = dbmod.open_db(db_path)
    try:
        query = "SELECT * FROM nights WHERE id = ?" if night_id else \
            "SELECT * FROM nights WHERE status IN ('done', 'bad_night', 'skipped', 'failed') ORDER BY id DESC LIMIT 1"
        row = conn.execute(query, (night_id,) if night_id else ()).fetchone()
        if not row:
            return "No morning report yet. Run /sleep now."
        return render_report(dbmod.loads(row["report_json"], {}))
    finally:
        conn.close()


def brain_list_text(db_path: str) -> str:
    conn = dbmod.open_db(db_path)
    try:
        rows = conn.execute("SELECT * FROM brains ORDER BY version").fetchall()
        if not rows:
            return "No brains yet. The first one is trained during the first night."
        lines = ["Brains"]
        for b in rows:
            exam = dbmod.loads(b["exam_json"], {}).get("candidate", {})
            fams = ", ".join(f"{f} ({s.get('state')})" for f, s in dbmod.loads(b["families_json"], {}).items()) or "-"
            lines.append(f"  v{b['version']:<3} {b['status']:<9} parent {('v' + str(b['parent_version'])) if b['parent_version'] else 'base':<5} "
                         f"held-out {exam.get('heldout_acc', 0) * 100:.0f}%  probes {exam.get('probe_pass', 0) * 100:.0f}%  "
                         f"manifest {str(b['manifest_hash'])[:10]}  families {fams}")
        return "\n".join(lines)
    finally:
        conn.close()
