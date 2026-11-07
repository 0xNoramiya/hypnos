"""Slash and CLI commands. Handlers return text and never block: anything slow is spawned elsewhere."""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import db as dbmod
from .habits import refresh as refresh_habits
from .signals import sleep_pressure

HYPNOS_HELP = """Hypnos learns your habits while Hermes sleeps.

  /hypnos status        sleep pressure, today's signals, current brain
  /hypnos on | off      start or stop capture and routing
  /hypnos habits        the habit ledger and what each brain has consolidated
  /hypnos habits refresh   re-read pairs, repeats and MEMORY.md / USER.md
  /hypnos good [note]   mark the last reply as good
  /hypnos bad [note]    mark the last reply as bad
"""


def bar(percent: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(100.0, percent)) / 100 * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = max(0, time.time() - ts)
    if delta < 90:
        return "just now"
    if delta < 5400:
        return f"{int(delta // 60)} min ago"
    if delta < 172800:
        return f"{int(delta // 3600)} h ago"
    return f"{int(delta // 86400)} days ago"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


@dataclass
class Status:
    enabled: bool
    pressure: float
    weight: float
    counts: dict[str, int]
    turns: int
    tool_calls: int
    pairs: int
    gold_pairs: int
    brain: dict | None
    last_night: dict | None
    last_activity: float | None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def collect_status(conn, settings: dict) -> Status:
    pressure = sleep_pressure(conn, float(settings.get("pressure_scale") or 25.0))
    last_night = conn.execute(
        "SELECT id, started_at, ended_at, status, brain_version FROM nights ORDER BY id DESC LIMIT 1").fetchone()
    since = last_night["ended_at"] if last_night and last_night["ended_at"] else 0
    turns = conn.execute("SELECT COUNT(*) FROM episodes WHERE started_at > ?", (since,)).fetchone()[0]
    tool_calls = conn.execute(
        "SELECT COUNT(*) FROM steps s JOIN episodes e ON e.id = s.episode_id WHERE s.kind = 'tool' AND e.started_at > ?",
        (since,)).fetchone()[0]
    pair_rows = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(gold), 0) FROM pairs WHERE night_id IS NULL AND excluded = 0").fetchone()
    brain = conn.execute(
        "SELECT version, base_model, status, promoted_at, families_json FROM brains WHERE status = 'promoted' "
        "ORDER BY version DESC LIMIT 1").fetchone()
    return Status(
        enabled=bool(settings.get("enabled", True)),
        pressure=pressure.percent,
        weight=pressure.total_weight,
        counts=pressure.counts,
        turns=int(turns),
        tool_calls=int(tool_calls),
        pairs=int(pair_rows[0] or 0),
        gold_pairs=int(pair_rows[1] or 0),
        brain=dict(brain) if brain else None,
        last_night=dict(last_night) if last_night else None,
        last_activity=dbmod.get_meta(conn, "last_activity"),
    )


def render_habits(conn) -> str:
    rows = conn.execute(
        "SELECT h.*, (SELECT COUNT(*) FROM probes p WHERE p.habit_id = h.id) AS n_probes, "
        "(SELECT COUNT(*) FROM probes p WHERE p.habit_id = h.id AND p.kind = 'judged') AS n_judged "
        "FROM habits h WHERE h.excluded = 0 ORDER BY h.id").fetchall()
    if not rows:
        return ("No habits yet. Hypnos learns them from your corrections and denials, from patterns you repeat, "
                "and from preference lines in MEMORY.md and USER.md.")
    brain = conn.execute("SELECT MAX(version) FROM brains WHERE status = 'promoted'").fetchone()[0]
    lines = [f"Habits ({len(rows)})" + (f", brain v{brain}" if brain else "")]
    for h in rows:
        evidence = len(dbmod.loads(h["evidence_json"], []))
        state = "awake"
        if brain:
            res = conn.execute("SELECT passes, total, consolidated FROM habit_results WHERE habit_id = ? "
                               "AND brain_version = ? AND with_text = 0", (h["id"], brain)).fetchone()
            if res:
                state = f"{'consolidated' if res['consolidated'] else 'learning'} {res['passes']}/{res['total']}"
        probe_note = f"{h['n_probes']} probes" + (f" ({h['n_judged']} judged)" if h["n_judged"] else "")
        lines.append(f"  #{h['id']:<3} {h['statement']}")
        lines.append(f"       {h['source']}, {_plural(evidence, 'episode')} of evidence, {probe_note}, {state}")
    return "\n".join(lines)


def render_status(st: Status) -> str:
    c = st.counts
    lines = [f"Hypnos {__version__}  {'capture on' if st.enabled else 'off (run /hypnos on)'}"]
    lines.append(f"Sleep pressure  {bar(st.pressure)} {st.pressure:.0f}%")
    details = []
    for kind, word in (("approval_denied", "denial"), ("correction", "correction"), ("reask", "re-ask"),
                       ("test_fail", "failing test run"), ("mark_bad", "bad mark")):
        if c.get(kind):
            details.append(_plural(c[kind], word))
    if st.pairs:
        details.append(f"{_plural(st.pairs, 'learning pair')} ({st.gold_pairs} gold)")
    lines.append("  since last night: " + (", ".join(details) if details else "nothing to learn yet"))
    lines.append(f"Awake           {_plural(st.turns, 'turn')}, {_plural(st.tool_calls, 'tool call')}, "
                 f"last activity {_ago(st.last_activity)}")
    if st.brain:
        lines.append(f"Brain           v{st.brain['version']} on {st.brain['base_model']}, promoted {_ago(st.brain['promoted_at'])}")
    else:
        lines.append("Brain           none yet. Every turn goes to your normal model.")
    if st.last_night:
        n = st.last_night
        lines.append(f"Last night      #{n['id']} {n['status']}, {_ago(n['ended_at'] or n['started_at'])}")
    else:
        lines.append("Last night      never slept")
    return "\n".join(lines)


class Commands:
    def __init__(self, *, db_path: Callable[[], str], settings: Callable[[], dict],
                 set_config: Callable[[str, Any], None], mark: Callable[[bool, str, str], None],
                 flush: Callable[[], Any] = lambda: None, memories_dir: Callable[[], Path | None] = lambda: None,
                 on_habits_changed: Callable[[], Any] = lambda: None):
        self.memories_dir = memories_dir
        self.on_habits_changed = on_habits_changed
        self.db_path = db_path
        self.settings = settings
        self.set_config = set_config
        self.mark = mark
        self.flush = flush

    def _conn(self):
        path = self.db_path()
        if not Path(path).exists():
            return dbmod.open_db(path)
        conn = dbmod.connect(path)
        dbmod.migrate(conn)
        return conn

    def status(self) -> str:
        self.flush()
        conn = self._conn()
        try:
            return render_status(collect_status(conn, self.settings()))
        finally:
            conn.close()

    def hypnos(self, raw_args: str = "", session_id: str = "") -> str:
        try:
            args = shlex.split(raw_args or "")
        except ValueError:
            args = (raw_args or "").split()
        sub = args[0].lower() if args else "status"
        rest = " ".join(args[1:])
        if sub in ("status", "st"):
            return self.status()
        if sub in ("on", "off"):
            self.set_config("enabled", sub == "on")
            invalidate = getattr(self.settings, "invalidate", None)
            if invalidate:
                invalidate()
            return "Hypnos is on. Capture and routing resume." if sub == "on" else (
                "Hypnos is off. Nothing is captured and every turn goes to your normal model.")
        if sub == "habits":
            self.flush()
            conn = self._conn()
            try:
                note = ""
                if rest.strip().lower() == "refresh":
                    result = refresh_habits(conn, self.memories_dir())
                    self.on_habits_changed()
                    note = (f"Refreshed: {len(result.created)} new, {len(result.updated)} updated, "
                            f"{result.skipped_declarative} declarative memory lines left in text memory.\n\n")
                return note + render_habits(conn)
            finally:
                conn.close()
        if sub in ("good", "bad"):
            self.mark(sub == "good", session_id, rest)
            return f"Marked the last reply as {sub}." + (" It counts toward tonight's sleep." if sub == "bad" else "")
        if sub in ("help", "-h", "--help"):
            return HYPNOS_HELP
        return f"Unknown subcommand '{sub}'.\n\n{HYPNOS_HELP}"
