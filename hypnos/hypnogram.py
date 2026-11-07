"""Hypnogram: stage segments from night events, a text chart for the terminal, JSON for the Desktop."""

from __future__ import annotations

import time

from . import db as dbmod

ORDER = ["WAKE", "REM", "N1", "N2", "N3"]
LABELS = {"WAKE": "Wake", "REM": "REM", "N1": "N1", "N2": "N2", "N3": "N3"}


def latest_night(conn, night_id: int | None = None):
    if night_id:
        return conn.execute("SELECT * FROM nights WHERE id = ?", (night_id,)).fetchone()
    return conn.execute("SELECT * FROM nights ORDER BY id DESC LIMIT 1").fetchone()


def segments(conn, night_id: int) -> list[dict]:
    events = conn.execute("SELECT * FROM night_events WHERE night_id = ? ORDER BY id", (night_id,)).fetchall()
    night = conn.execute("SELECT * FROM nights WHERE id = ?", (night_id,)).fetchone()
    end_ts = night["ended_at"] or (time.time() if night["status"] in ("running", "cancelling") else
                                   (events[-1]["ts"] if events else night["started_at"]))
    out: list[dict] = []
    for ev in events:
        detail = dbmod.loads(ev["detail_json"], {})
        if out and out[-1]["stage"] == ev["stage"] and out[-1]["cycle"] == (ev["cycle"] or out[-1]["cycle"]):
            out[-1]["events"].append({"event": ev["event"], "ts": ev["ts"], **detail})
            continue
        if out:
            out[-1]["end"] = ev["ts"]
        out.append({"stage": ev["stage"], "cycle": ev["cycle"], "start": ev["ts"], "end": None,
                    "events": [{"event": ev["event"], "ts": ev["ts"], **detail}]})
    if out:
        out[-1]["end"] = max(end_ts, out[-1]["start"])
    return out


def night_payload(conn, night_id: int | None = None) -> dict:
    night = latest_night(conn, night_id)
    if night is None:
        return {"night": None, "segments": []}
    segs = segments(conn, night["id"])
    current = segs[-1] if segs else None
    progress = None
    if current:
        for ev in reversed(current["events"]):
            if ev.get("event") == "progress":
                progress = {"step": ev.get("step"), "steps": ev.get("steps"), "loss": ev.get("loss")}
                break
    return {"night": {"id": night["id"], "status": night["status"], "started_at": night["started_at"],
                      "ended_at": night["ended_at"], "trigger": night["trigger"],
                      "brain_version": night["brain_version"]},
            "segments": segs, "current_stage": current["stage"] if current else None,
            "current_cycle": current["cycle"] if current else None, "progress": progress,
            "order": ORDER}


def render_text(payload: dict, width: int = 56) -> str:
    night = payload.get("night")
    if not night:
        return "No nights yet. Run /sleep now when sleep pressure is high."
    segs = payload["segments"]
    if not segs:
        return f"Night #{night['id']} has no stage events yet."
    t0 = segs[0]["start"]
    t1 = max(s["end"] or s["start"] for s in segs)
    span = max(1e-6, t1 - t0)
    grid = {stage: [" "] * width for stage in ORDER}
    for seg in segs:
        a = int((seg["start"] - t0) / span * (width - 1))
        b = max(a, int(((seg["end"] or seg["start"]) - t0) / span * (width - 1)))
        row = grid.get(seg["stage"])
        if row is None:
            continue
        for col in range(a, b + 1):
            row[col] = "#"
    minutes = span / 60
    status = night["status"]
    head = f"Hypnogram  night #{night['id']}  {minutes:.1f} min  {status}"
    if payload.get("current_stage") and status in ("running", "cancelling"):
        head += f"  now: {payload['current_stage']}"
        if payload.get("progress"):
            p = payload["progress"]
            head += f" step {p['step']}/{p['steps']}"
    lines = [head]
    for stage in ORDER:
        lines.append(f"{LABELS[stage]:>4} |{''.join(grid[stage])}|")
    cycles = sorted({s["cycle"] for s in segs if s["cycle"]})
    if cycles:
        lines.append(f"     cycles: {len(cycles)}")
    return "\n".join(lines)
