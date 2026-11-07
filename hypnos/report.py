"""The morning report, styled like a sleep study (polysomnography) report.

Definitions:
- Total sleep time: lights out to waking up.
- Stage minutes: time between consecutive stage events, attributed to the earlier event's stage.
- Sleep efficiency: time in N3 and REM (actual training) divided by total sleep time. Clinically it is
  time asleep over time in bed; here "asleep" means "doing useful training steps".
- Tokens saved per turn: the frontier's prompt tokens (measured by routing, else the Hermes v0.23
  default of about 12k) minus the brain's compact prompt with consolidated habits pruned.
"""

from __future__ import annotations

import itertools
import time
from collections import defaultdict

from . import db as dbmod
from .brainfmt import brain_tools, build_messages, estimate_tokens

DEFAULT_FRONTIER_TOKENS = 12000


def stage_minutes(events: list) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for current, nxt in itertools.pairwise(events):
        out[current["stage"]] += max(0.0, nxt["ts"] - current["ts"]) / 60
    return {k: round(v, 2) for k, v in out.items()}


def brain_prompt_tokens(statements: list[str]) -> int:
    msgs = build_messages("Install axios.", [], statements)
    return estimate_tokens(msgs) + estimate_tokens(brain_tools(None, "packages"))


def build_report(conn, night_id: int, version: int | None) -> dict:
    night = conn.execute("SELECT * FROM nights WHERE id = ?", (night_id,)).fetchone()
    events = conn.execute("SELECT * FROM night_events WHERE night_id = ? ORDER BY id", (night_id,)).fetchall()
    brain = conn.execute("SELECT * FROM brains WHERE version = ?", (version,)).fetchone() if version else None
    exam = dbmod.loads(brain["exam_json"], {}) if brain else {}
    decision = exam.get("decision", {})
    end_ts = events[-1]["ts"] if events else time.time()
    total_min = round(max(0.0, end_ts - night["started_at"]) / 60, 2)
    minutes = stage_minutes([dict(e) for e in events] + [{"stage": "WAKE", "ts": end_ts}])
    training = minutes.get("N3", 0) + minutes.get("REM", 0)
    cycles = max([e["cycle"] or 0 for e in events] or [0])
    habits = conn.execute("SELECT id, statement, family FROM habits WHERE status = 'active' AND excluded = 0").fetchall()
    # Habits only move into the weights when the brain that learned them is promoted. After a bad night the
    # previous brain stays, so the report shows the habits that brain already holds.
    kept_version = None
    if decision.get("promote"):
        consolidated_ids = set(decision.get("consolidated", []))
    else:
        kept_version = brain["parent_version"] if brain else None
        consolidated_ids = {r[0] for r in conn.execute(
            "SELECT habit_id FROM habit_results WHERE brain_version = ? AND with_text = 0 AND consolidated = 1",
            (kept_version,))} if kept_version else set()
    consolidated = [h for h in habits if h["id"] in consolidated_ids]
    awake = [h for h in habits if h["id"] not in consolidated_ids]
    frontier = conn.execute("SELECT AVG(frontier_prompt_tokens) FROM routes WHERE frontier_prompt_tokens > 0").fetchone()[0]
    frontier_tokens = int(frontier) if frontier else DEFAULT_FRONTIER_TOKENS
    full_brain = brain_prompt_tokens([h["statement"] for h in habits])
    pruned_brain = brain_prompt_tokens([h["statement"] for h in awake])
    synthetic = conn.execute("SELECT COUNT(*) FROM episodes WHERE source = 'synthetic'").fetchone()[0] > 0
    nightmares = [dict(e) for e in events if e["event"] in ("nightmare", "nightmare_resolved")]
    resolved = sum(1 for e in nightmares if e["event"] == "nightmare_resolved")
    found = sum(1 for e in nightmares if e["event"] == "nightmare")
    cand, base = exam.get("candidate", {}), exam.get("baseline", {})
    mode_event = next((dbmod.loads(e["detail_json"], {}) for e in events if e["event"] == "dream_mode"), {})
    spend = dbmod.loads(night["spend_json"], {}) or {}
    return {
        "teacher_tokens": int(spend.get("teacher_tokens") or 0), "teacher_usd_est": spend.get("teacher_usd_est"),
        "rem_mode": mode_event.get("mode", ""), "rem_mode_reason": mode_event.get("reason", ""),
        "teacher": mode_event.get("teacher", ""),
        "night_id": night_id,
        "date": time.strftime("%Y-%m-%d", time.localtime(night["started_at"])),
        "started_at": night["started_at"], "ended_at": end_ts,
        "synthetic": synthetic,
        "trigger": night["trigger"],
        "model": brain["base_model"] if brain else "",
        "total_sleep_min": total_min,
        "cycles": cycles,
        "stage_min": minutes,
        "n3_share": round(minutes.get("N3", 0) / training, 3) if training else 0.0,
        "rem_share": round(minutes.get("REM", 0) / training, 3) if training else 0.0,
        "sleep_efficiency": round(training / total_min, 3) if total_min else 0.0,
        "nightmares": found, "nightmares_resolved": resolved,
        "habits_consolidated": [{"id": h["id"], "statement": h["statement"]} for h in consolidated],
        "kept_version": kept_version,
        "habits_awake": [{"id": h["id"], "statement": h["statement"]} for h in awake],
        "frontier_prompt_tokens": frontier_tokens,
        "brain_prompt_tokens": pruned_brain,
        "pruned_tokens": max(0, full_brain - pruned_brain),
        "tokens_saved_per_turn": max(0, frontier_tokens - pruned_brain),
        "version": version,
        "baseline_label": exam.get("baseline_label", ""),
        "exam": {"candidate": cand, "baseline": base},
        "promoted": bool(decision.get("promote")),
        "reasons": decision.get("reasons", []),
        "families": decision.get("families", []),
        "train": exam.get("train", {}),
    }


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _delta(cand: dict, base: dict, key: str) -> str:
    c, b = cand.get(key), base.get(key)
    if c is None:
        return "-"
    if b is None:
        return _pct(c)
    sign = "+" if c >= b else ""
    return f"{_pct(b)} -> {_pct(c)} ({sign}{(c - b) * 100:.0f} pts)"


def render_report(r: dict) -> str:
    if not r or r.get("status") in ("skipped", "failed", "cancelled"):
        return f"Hypnos: {r.get('reason', 'no report')}" if r else "Hypnos: no report yet."
    lines = []
    title = f"HYPNOS SLEEP STUDY  Night #{r['night_id']}  {r['date']}"
    if r.get("synthetic"):
        title += "  [SYNTHETIC DEMO DATA]"
    lines += [title, "=" * len(title)]
    sm = r.get("stage_min", {})
    lines.append(f"Total sleep time   {r['total_sleep_min']:.1f} min, {r['cycles']} cycles, brain {r['model']}")
    lines.append(f"Stages             N1 {sm.get('N1', 0):.1f}  N2 {sm.get('N2', 0):.1f}  N3 {sm.get('N3', 0):.1f}  "
                 f"REM {sm.get('REM', 0):.1f}  wake {sm.get('WAKE', 0):.1f} (min)")
    lines.append(f"N3 / REM split     {_pct(r['n3_share'])} / {_pct(r['rem_share'])} of training time")
    lines.append(f"Sleep efficiency   {_pct(r['sleep_efficiency'])} (training time / total sleep time)")
    if r.get("rem_mode"):
        mode = "sandbox (Docker, no network)" if r["rem_mode"] == "sandbox" else f"lucid, no execution ({r['rem_mode_reason']})"
        spent = f", {r['teacher_tokens']:,} teacher tokens" if r.get("teacher_tokens") else ""
        if r.get("teacher_usd_est"):
            spent += f" (about ${r['teacher_usd_est']:.2f})"
        lines.append(f"REM dreams         {mode}, teacher: {r.get('teacher') or '-'}{spent}")
    if r.get("nightmares"):
        lines.append(f"Nightmares         {r['nightmares_resolved']} of {r['nightmares']} resolved")
    lines.append("")
    lines.append(f"Waking exam        v{r['version']} vs {r['baseline_label']}")
    cand, base = r["exam"]["candidate"], r["exam"]["baseline"]
    lines.append(f"  held-out next action  {_delta(cand, base, 'heldout_acc')}  (n={cand.get('heldout_n', 0)})")
    lines.append(f"  habit probes          {_delta(cand, base, 'probe_pass')}  (n={cand.get('probe_n', 0)})")
    if cand.get("dream_n"):
        lines.append(f"  dream verifiers       {_delta(cand, base, 'dream_pass')}  (n={cand.get('dream_n', 0)})")
    lines.append(f"  valid outputs         {_delta(cand, base, 'validity')}")
    lines.append(f"  primary, lower bound  {_pct(cand.get('primary_lower'))}  vs baseline {_pct(base.get('primary'))}")
    lines.append("")
    if r["habits_consolidated"]:
        if r.get("promoted") or not r.get("kept_version"):
            lines.append(f"Consolidated into the weights ({len(r['habits_consolidated'])}), pruned from the brain's prompt:")
        else:
            lines.append(f"Still in the weights of v{r['kept_version']} ({len(r['habits_consolidated'])}), "
                         "pruned from its prompt:")
        lines += [f"  + {h['statement']}" for h in r["habits_consolidated"]]
    if r["habits_awake"]:
        lines.append(f"Still learning ({len(r['habits_awake'])}), kept as text:")
        lines += [f"  . {h['statement']}" for h in r["habits_awake"]]
    lines.append("")
    lines.append(f"Prompt tokens      frontier ~{r['frontier_prompt_tokens']:,}, brain {r['brain_prompt_tokens']:,} "
                 f"({r['pruned_tokens']} pruned), ~{r['tokens_saved_per_turn']:,} saved per routed turn")
    verdict = "PROMOTED" if r["promoted"] else "BAD NIGHT, kept the previous brain"
    lines.append(f"Verdict            {verdict}")
    for reason in r.get("reasons", []):
        lines.append(f"  - {reason}")
    if r["promoted"] and r.get("families"):
        lines.append(f"Families in shadow mode: {', '.join(r['families'])}")
    return "\n".join(lines)
