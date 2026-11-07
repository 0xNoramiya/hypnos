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
        retention_days=int(settings.get("retention_days") or 90),
        teacher_usd_per_mtok=float(settings.get("teacher_usd_per_mtok") or 0),
        trigger=trigger,
        **({"max_steps": 40, "max_heldout": 20, "probes_per_habit": 6, "epochs_sft": 3.0, "epochs_dpo": 3.0,
            "hyper": {"batch_size": 4, "grad_accum": 1, "max_len": 768, "max_new_tokens": 64, "gen_batch": 16}}
           if tiny else {}))


def run_sleep(*, db_path: str, data_dir: Path, memories_dir: Path | None, settings: dict, tiny: bool = False,
              cycles: int | None = None, max_minutes: float | None = None, trigger: str = "manual",
              if_idle: float | None = None, broadcast: Callable[[str, dict], None] | None = None,
              out: Callable[[str], None] = print, quiet: bool = False, llm: object = None) -> int:
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
    from .dreams import Dreamer, LLMTeacher, ScriptedTeacher
    mode = str(settings.get("teacher") or "auto")
    scripted = ScriptedTeacher(seed=int(time.time()))
    budget = teacher_budget(settings)
    teacher = scripted if (mode == "scripted" or llm is None) else LLMTeacher(llm, budget, fallback=scripted)
    dreamer = Dreamer(teacher, k=int(settings.get("dream_variants") or 4) if not tiny else 3,
                      use_sandbox=bool(settings.get("sandbox", True)))
    night = Night(db_path, data_dir, cfg, memories_dir=memories_dir, broadcast=broadcast, log=log, dreamer=dreamer)
    try:
        report = night.run()
    except NightError as exc:
        out(f"Hypnos could not sleep: {exc}")
        return 1
    if report.get("promoted") and report.get("version"):
        conn = dbmod.open_db(db_path)
        brain = conn.execute("SELECT * FROM brains WHERE version = ?", (report["version"],)).fetchone()
        conn.close()
        sync_server(data_dir, dict(brain) if brain else None, settings, allow_start=True, out=out)
    out("")
    out(render_report(report))
    return 0


def teacher_budget(settings: dict) -> int:
    """Night token budget for the teacher: the token cap, or the dollar cap at the configured price, whichever is lower."""
    tokens = int(settings.get("teacher_budget_tokens") or 60000)
    price = float(settings.get("teacher_usd_per_mtok") or 0)
    usd = float(settings.get("night_max_usd") or 0)
    if price > 0 and usd > 0:
        tokens = min(tokens, int(usd / price * 1_000_000))
    return max(0, tokens)


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


def rollback(db_path: str, version: int) -> tuple[str, dict | None]:
    """Make an earlier brain current again (``0`` means no brain). Returns (message, brain to serve)."""
    conn = dbmod.open_db(db_path)
    try:
        current = conn.execute("SELECT * FROM brains WHERE status = 'promoted' ORDER BY version DESC LIMIT 1").fetchone()
        if version == 0:
            if current:
                conn.execute("UPDATE brains SET status = 'retired' WHERE version = ?", (current["version"],))
            return "Rolled back to no brain. Every turn goes to your normal model.", None
        target = conn.execute("SELECT * FROM brains WHERE version = ?", (version,)).fetchone()
        if target is None:
            return f"There is no brain v{version}.", None
        if target["status"] == "rejected" or not target["promoted_at"]:
            return f"Brain v{version} never passed its exam, so it cannot be restored.", None
        if current and current["version"] == version:
            return f"Brain v{version} is already current.", dict(target)
        conn.execute("BEGIN IMMEDIATE")
        if current:
            conn.execute("UPDATE brains SET status = 'retired' WHERE version = ?", (current["version"],))
        conn.execute("UPDATE brains SET status = 'promoted', promoted_at = ? WHERE version = ?", (time.time(), version))
        conn.execute("COMMIT")
        was = f" (was v{current['version']})" if current else ""
        return f"Rolled back to brain v{version}{was}.", dict(target)
    finally:
        conn.close()


def sync_server(data_dir: Path, brain: dict | None, settings: dict, *, allow_start: bool = False,
                out: Callable[[str], None] = lambda s: None) -> None:
    """Point the brain server at ``brain``, or stop it when there is none.

    Starts a server only when ``allow_start`` is set (after a promoted night), routing is on, the user has
    not configured their own endpoint, and ``auto_start_brain_server`` is enabled.
    """
    from . import brainserver
    if not brainserver.status(data_dir).get("running"):
        if (allow_start and brain is not None and settings.get("routing", "auto") != "off"
                and not settings.get("brain_endpoint") and settings.get("auto_start_brain_server", True)):
            out(f"Starting the brain server for v{brain['version']} on 127.0.0.1:{settings.get('brain_port') or 8765}...")
            try:
                brainserver.start(data_dir, base_model=brain["base_model"], adapter=brain["adapter_path"],
                                  version=brain["version"], trainer_python=str(settings.get("trainer_python") or ""),
                                  port=int(settings.get("brain_port") or brainserver.DEFAULT_PORT))
            except Exception as exc:
                out(f"Could not start the brain server: {exc}")
        return
    if brain is None:
        brainserver.stop(data_dir)
        return
    try:
        brainserver.swap(data_dir, brain["adapter_path"], brain["version"])
    except Exception:
        brainserver.stop(data_dir)


# --- Lethe -----------------------------------------------------------------------------------------------

LETHE_USAGE = ("Usage: /lethe session <id> | /lethe habit <id> | /lethe search <text>, then /lethe confirm <code>.\n"
               "Lethe forgets the data exactly: brains that trained on it are retired and a clean one is retrained.")


def lethe_plan_text(db_path: str, kind: str, target: str) -> str:
    from . import lineage
    conn = dbmod.open_db(db_path)
    try:
        plan = lineage.plan(conn, kind, target)
        if not (plan.episodes or plan.pairs or plan.habits or plan.dreams):
            return f"Nothing matches {kind} {target}."
        code = lineage.stage(conn, plan)
        return f"{plan.summary()}\nTo go ahead, run /lethe confirm {code} within 15 minutes."
    finally:
        conn.close()


def lethe_apply(db_path: str, plan) -> tuple[bool, str]:
    """Exclude the data now. Returns (needs_retrain, message)."""
    from . import lineage
    conn = dbmod.open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        lineage.exclude(conn, plan)
        current = conn.execute("SELECT version FROM brains WHERE status = 'promoted'").fetchone()
        needs = bool(plan.affected) and bool(plan.retrain_episodes or plan.retrain_pairs)
        if plan.affected and not needs:
            lineage.retire_contaminated(conn, plan, None)
        if plan.affected and current and current["version"] not in plan.affected:
            needs = False
            lineage.retire_contaminated(conn, plan, current["version"])
        conn.execute("COMMIT")
        msg = (f"Forgot {plan.kind} {plan.target}: {len(plan.episodes)} turns, {len(plan.pairs)} pairs, "
               f"{len(plan.habits)} habits and {len(plan.dreams)} dreams are excluded from now on.")
        return needs, msg
    finally:
        conn.close()


def lethe_run(*, db_path: str, data_dir: Path, memories_dir: Path | None, settings: dict, plan,
              out: Callable[[str], None] = print) -> int:
    cfg = night_config(settings, trigger="lethe")
    night = Night(db_path, data_dir, cfg, memories_dir=memories_dir, log=out, lethe=plan)
    try:
        report = night.run()
    except NightError as exc:
        out(f"Lethe could not retrain: {exc}")
        return 1
    if report.get("version"):
        conn = dbmod.open_db(db_path)
        brain = conn.execute("SELECT * FROM brains WHERE status = 'promoted' ORDER BY version DESC LIMIT 1").fetchone()
        conn.close()
        sync_server(data_dir, dict(brain) if brain else None, settings, allow_start=bool(brain), out=out)
    out(render_report(report))
    return 0


def lethe_slash(raw: str, *, db_path: str, data_dir: Path, spawn: Callable[[list[str], Path], int] | None = None) -> str:
    from . import lineage
    words = (raw or "").split(maxsplit=1)
    if not words:
        return LETHE_USAGE
    kind, rest = words[0].lower(), (words[1].strip() if len(words) > 1 else "")
    if kind == "confirm":
        conn = dbmod.open_db(db_path)
        try:
            plan = lineage.take_pending(conn, rest)
        finally:
            conn.close()
        if plan is None:
            return "That confirmation code is unknown or expired. Run the /lethe request again."
        needs, msg = lethe_apply(db_path, plan)
        if not needs:
            return msg + " No brain needs retraining."
        conn = dbmod.open_db(db_path)
        dbmod.set_meta(conn, "lethe_running", plan.as_dict())
        conn.close()
        pid = (spawn or _spawn_lethe)(["run"], data_dir)
        return msg + f" Retraining a clean brain in the background (pid {pid}); /hypnos report shows the result."
    if kind not in ("session", "habit", "search") or not rest:
        return LETHE_USAGE
    return lethe_plan_text(db_path, kind, rest)


def _spawn_lethe(args: list[str], data_dir: Path) -> int:
    root = hermes_root()
    log = open(data_dir / "nights" / "lethe.log", "a", encoding="utf-8") if (data_dir / "nights").exists() else subprocess.DEVNULL
    proc = subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "hypnos", "lethe", *args],
                            cwd=str(root) if root else None, env=os.environ.copy(), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid
