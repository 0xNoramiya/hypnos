"""Data for the Desktop plugin. Every function opens the profile's database and returns plain dicts."""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from . import db as dbmod
from .commands import collect_status
from .hypnogram import night_payload
from .report import build_report
from .signals import SIGNALS


def _conn(db_path: str):
    return dbmod.open_db(db_path)


def status(db_path: str, settings: dict) -> dict:
    conn = _conn(db_path)
    try:
        st = collect_status(conn, settings).as_dict()
        running = conn.execute("SELECT id FROM nights WHERE status IN ('running', 'cancelling') ORDER BY id DESC LIMIT 1").fetchone()
        st["sleeping"] = bool(running)
        if running:
            payload = night_payload(conn, running["id"])
            st["night"] = {"id": running["id"], "stage": payload.get("current_stage"), "cycle": payload.get("current_cycle"),
                           "progress": payload.get("progress")}
        last = conn.execute("SELECT turn_id, MAX(ts) AS ts FROM routes WHERE turn_id IS NOT NULL").fetchone()
        st["last_turn"] = turn(conn, last["turn_id"]) if last and last["turn_id"] else None
        st["habits"] = {"total": conn.execute("SELECT COUNT(*) FROM habits WHERE status = 'active' AND excluded = 0").fetchone()[0]}
        if st.get("brain"):
            st["brain"]["families"] = dbmod.loads(st["brain"].pop("families_json", "{}"), {})
            st["habits"]["consolidated"] = conn.execute(
                "SELECT COUNT(*) FROM habit_results WHERE brain_version = ? AND with_text = 0 AND consolidated = 1",
                (st["brain"]["version"],)).fetchone()[0]
        unread = conn.execute("SELECT id FROM nights WHERE status IN ('done', 'bad_night') ORDER BY id DESC LIMIT 1").fetchone()
        st["latest_report"] = unread["id"] if unread else None
        return st
    finally:
        conn.close()


def night(db_path: str, night_id: int | None = None) -> dict:
    conn = _conn(db_path)
    try:
        payload = night_payload(conn, night_id)
        payload["running"] = bool(payload.get("night")) and payload["night"]["status"] in ("running", "cancelling")
        return payload
    finally:
        conn.close()


def nights(db_path: str, limit: int = 30) -> list[dict]:
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT id, started_at, ended_at, status, trigger, brain_version FROM nights "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def report(db_path: str, night_id: int | None = None) -> dict:
    conn = _conn(db_path)
    try:
        if night_id is None:
            row = conn.execute("SELECT * FROM nights WHERE status IN ('done', 'bad_night') ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM nights WHERE id = ?", (night_id,)).fetchone()
        if row is None:
            return {"report": None}
        stored = dbmod.loads(row["report_json"], {})
        if row["status"] in ("done", "bad_night") and row["brain_version"]:
            stored = build_report(conn, row["id"], row["brain_version"]) or stored
        return {"report": stored, "night": dict(row) | {"report_json": None, "params_json": None}}
    finally:
        conn.close()


def turn(conn_or_path, turn_id: str) -> dict:
    conn = _conn(conn_or_path) if isinstance(conn_or_path, str) else conn_or_path
    try:
        rows = conn.execute("SELECT * FROM routes WHERE turn_id = ? ORDER BY id", (turn_id,)).fetchall()
        brain_calls = [r for r in rows if r["mode"] == "brain"]
        version = next((r["brain_version"] for r in brain_calls), None)
        saved = sum(int(r["tokens_saved"] or 0) for r in brain_calls)
        frontier_calls = [r for r in rows if r["mode"] in ("passthrough", "shadow", "fallback")]
        if brain_calls and not frontier_calls:
            answered = "brain"
        elif brain_calls:
            answered = "mixed"
        else:
            answered = "frontier"
        return {"turn_id": turn_id, "answered_by": answered, "brain_version": version, "tokens_saved": saved,
                "calls": len(rows), "brain_calls": len(brain_calls),
                "shadow": any(r["mode"] == "shadow" for r in rows),
                "fallbacks": [r["fallback_reason"] for r in rows if r["mode"] == "fallback"]}
    finally:
        if isinstance(conn_or_path, str):
            conn.close()


def sessions(db_path: str, limit: int = 200) -> dict:
    """Per-session brain usage, keyed by session id, for sidebar row badges (one request for all rows)."""
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT session_id, MAX(brain_version) AS v, COUNT(DISTINCT turn_id) AS turns, SUM(tokens_saved) AS saved "
            "FROM routes WHERE mode = 'brain' AND session_id IS NOT NULL GROUP BY session_id "
            "ORDER BY MAX(ts) DESC LIMIT ?", (limit,)).fetchall()
        out = {r["session_id"]: {"version": r["v"], "turns": r["turns"], "saved": int(r["saved"] or 0)} for r in rows}
        links = conn.execute("SELECT session_id, linked_from FROM sessions WHERE linked_from IS NOT NULL").fetchall()
        for link in links:
            if link["session_id"] in out and link["linked_from"] not in out:
                out[link["linked_from"]] = out[link["session_id"]]
        return out
    finally:
        conn.close()


def habits(db_path: str) -> list[dict]:
    conn = _conn(db_path)
    try:
        brain = conn.execute("SELECT MAX(version) FROM brains WHERE status = 'promoted'").fetchone()[0]
        out = []
        for h in conn.execute("SELECT * FROM habits WHERE excluded = 0 ORDER BY id"):
            res = conn.execute("SELECT passes, total, consolidated FROM habit_results WHERE habit_id = ? AND brain_version = ? "
                               "AND with_text = 0", (h["id"], brain)).fetchone() if brain else None
            out.append({"id": h["id"], "statement": h["statement"], "source": h["source"], "family": h["family"],
                        "evidence": len(dbmod.loads(h["evidence_json"], [])),
                        "consolidated": bool(res and res["consolidated"]),
                        "probe": {"passes": res["passes"], "total": res["total"]} if res else None})
        return out
    finally:
        conn.close()


def metrics(db_path: str) -> dict:
    """Per day: turns, average prompt tokens per turn, share answered by the brain, and task success."""
    conn = _conn(db_path)
    try:
        days: dict[str, dict] = defaultdict(lambda: {"turns": 0, "tokens": 0, "brain_turns": 0, "negative": 0,
                                                     "synthetic": False})
        episodes = conn.execute("SELECT id, turn_id, started_at, source FROM episodes WHERE excluded = 0").fetchall()
        day_of = {}
        for ep in episodes:
            day = time.strftime("%Y-%m-%d", time.localtime(ep["started_at"]))
            day_of[ep["turn_id"]] = day
            days[day]["turns"] += 1
            days[day]["synthetic"] |= ep["source"] == "synthetic"
        per_turn: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "brain": False})
        for r in conn.execute("SELECT turn_id, mode, frontier_prompt_tokens, brain_prompt_tokens FROM routes"):
            t = per_turn[r["turn_id"]]
            if r["mode"] == "brain":
                t["tokens"] += int(r["brain_prompt_tokens"] or 0)
                t["brain"] = True
            else:
                t["tokens"] += int(r["frontier_prompt_tokens"] or 0)
        for turn_id, t in per_turn.items():
            day = day_of.get(turn_id)
            if day is None:
                continue
            days[day]["tokens"] += t["tokens"]
            days[day]["brain_turns"] += int(t["brain"])
        negative_kinds = tuple(k for k, (pol, _) in SIGNALS.items() if pol < 0 and k not in ("tool_fail",))
        placeholders = ",".join("?" * len(negative_kinds))
        for row in conn.execute(
                f"SELECT e.started_at, COUNT(DISTINCT s.episode_id) AS n FROM signals s JOIN episodes e ON e.id = s.episode_id "
                f"WHERE s.kind IN ({placeholders}) GROUP BY e.id", negative_kinds):
            days[time.strftime("%Y-%m-%d", time.localtime(row["started_at"]))]["negative"] += 1
        series = []
        for day in sorted(days):
            d = days[day]
            turns = max(1, d["turns"])
            series.append({"day": day, "turns": d["turns"], "avg_prompt_tokens": round(d["tokens"] / turns),
                           "brain_share": round(d["brain_turns"] / turns, 3),
                           "success": round(1 - d["negative"] / turns, 3), "synthetic": d["synthetic"]})
        brains = [{"version": b["version"], "status": b["status"], "night_id": b["night_id"],
                   "exam": dbmod.loads(b["exam_json"], {}).get("candidate", {})}
                  for b in conn.execute("SELECT * FROM brains ORDER BY version")]
        return {"days": series, "brains": brains}
    finally:
        conn.close()


def start_sleep(db_path: str, data_dir: Path, tiny: bool = False) -> dict:
    from .sleepcmd import running_night, spawn_detached
    conn = _conn(db_path)
    try:
        current = running_night(conn)
        if current:
            return {"started": False, "night_id": current["id"], "message": "Already asleep."}
    finally:
        conn.close()
    pid = spawn_detached(["--trigger", "desktop", "--quiet"] + (["--tiny"] if tiny else []), data_dir)
    return {"started": True, "pid": pid}


def cancel_sleep(db_path: str) -> dict:
    from .night import request_cancel
    conn = _conn(db_path)
    try:
        return {"cancelled": request_cancel(conn)}
    finally:
        conn.close()
