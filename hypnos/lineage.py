"""Brain lineage: diffs, exports, and Lethe (exact unlearning by retraining from a clean ancestor).

Every brain's manifest lists the episodes, pairs, habits and dreams it trained on, and every brain trains
from its parent's adapter. To forget something exactly, Lethe:

1. marks the data excluded (it is never used again),
2. finds the earliest brain whose manifest contains it; that brain and all its descendants are
   contaminated,
3. retrains from the contaminated root's parent (or the base model) on the union of the contaminated
   brains' data, minus the excluded items,
4. runs the waking exam, retires every contaminated brain and deletes its adapter. If the retrained brain
   fails the exam, the clean parent becomes current, so forgotten data never stays in served weights.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbmod


@dataclass
class LethePlan:
    kind: str
    target: str
    episodes: list[int] = field(default_factory=list)
    pairs: list[int] = field(default_factory=list)
    habits: list[int] = field(default_factory=list)
    dreams: list[int] = field(default_factory=list)
    affected: list[int] = field(default_factory=list)
    parent: int | None = None
    retrain_episodes: list[int] = field(default_factory=list)
    retrain_pairs: list[int] = field(default_factory=list)
    code: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)

    def summary(self) -> str:
        what = f"{len(self.episodes)} turns, {len(self.pairs)} learning pairs, {len(self.habits)} habits, {len(self.dreams)} dreams"
        lines = [f"Lethe will forget {self.kind} {self.target}: {what}."]
        if self.affected:
            parent = f"brain v{self.parent}" if self.parent else "the base model"
            lines.append(f"Brains that saw this data: {', '.join(f'v{v}' for v in self.affected)}. They will be retired and "
                         f"their adapters deleted.")
            lines.append(f"A new brain is retrained from {parent} on {len(self.retrain_episodes)} turns and "
                         f"{len(self.retrain_pairs)} pairs, then takes the waking exam. This trains a model.")
        else:
            lines.append("No brain has trained on this data yet, so nothing needs retraining.")
        return "\n".join(lines)


def _brains(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM brains ORDER BY version").fetchall()


def _manifest(row) -> dict:
    return dbmod.loads(row["manifest_json"], {}) if row else {}


def targets(conn: sqlite3.Connection, kind: str, target: str) -> tuple[list[int], list[int], list[int], list[int]]:
    """(episodes, pairs, habits, dreams) selected by a Lethe request."""
    episodes: set[int] = set()
    pairs: set[int] = set()
    habits: set[int] = set()
    dreams: set[int] = set()
    if kind == "session":
        episodes |= {r[0] for r in conn.execute("SELECT id FROM episodes WHERE session_id = ? OR session_id IN "
                                                 "(SELECT session_id FROM sessions WHERE linked_from = ?)", (target, target))}
        pairs |= {r[0] for r in conn.execute("SELECT id FROM pairs WHERE session_id = ?", (target,))}
    elif kind == "habit":
        habit = conn.execute("SELECT * FROM habits WHERE id = ?", (int(target),)).fetchone()
        if habit is None:
            raise ValueError(f"There is no habit #{target}.")
        habits.add(habit["id"])
        episodes |= set(dbmod.loads(habit["evidence_json"], []))
        pairs |= {r[0] for r in conn.execute("SELECT id FROM pairs WHERE habit_id = ?", (habit["id"],))}
        dreams |= {r[0] for r in conn.execute("SELECT id FROM dreams WHERE habit_id = ?", (habit["id"],))}
    elif kind == "search":
        needle = f"%{target.lower()}%"
        episodes |= {r[0] for r in conn.execute(
            "SELECT DISTINCT e.id FROM episodes e LEFT JOIN steps s ON s.episode_id = e.id WHERE lower(e.user_message) LIKE ? "
            "OR lower(e.assistant_text) LIKE ? OR lower(s.args_json) LIKE ? OR lower(s.text) LIKE ?",
            (needle, needle, needle, needle))}
        pairs |= {r[0] for r in conn.execute("SELECT id FROM pairs WHERE lower(chosen_json) LIKE ? OR lower(rejected_json) LIKE ? "
                                             "OR lower(detail) LIKE ?", (needle, needle, needle))}
    else:
        raise ValueError("Lethe forgets a session, a habit, or turns matching a search.")
    if episodes:
        marks = ",".join("?" * len(episodes))
        pairs |= {r[0] for r in conn.execute(
            f"SELECT id FROM pairs WHERE context_episode IN ({marks}) OR chosen_step_id IN "
            f"(SELECT id FROM steps WHERE episode_id IN ({marks}))", (*episodes, *episodes))}
    return sorted(episodes), sorted(pairs), sorted(habits), sorted(dreams)


def plan(conn: sqlite3.Connection, kind: str, target: str) -> LethePlan:
    episodes, pairs, habits, dreams = targets(conn, kind, target)
    p = LethePlan(kind, target, episodes, pairs, habits, dreams)
    brains = _brains(conn)
    gone_e, gone_p, gone_h, gone_d = set(episodes), set(pairs), set(habits), set(dreams)
    root = None
    for b in brains:
        if b["status"] == "forgotten":
            continue
        m = _manifest(b)
        if (gone_e & set(m.get("episodes", []) + m.get("replay", [])) or gone_p & set(m.get("pairs", []))
                or gone_h & set(m.get("habits", [])) or gone_d & set((m.get("params") or {}).get("dreams", []))):
            root = b
            break
    if root is None:
        return p
    by_version = {b["version"]: b for b in brains}
    contaminated = {root["version"]}
    for b in brains:
        if b["version"] > root["version"] and b["parent_version"] in contaminated:
            contaminated.add(b["version"])
    p.affected = sorted(contaminated)
    p.parent = root["parent_version"]
    retrain_e: set[int] = set()
    retrain_p: set[int] = set()
    for v in p.affected:
        m = _manifest(by_version[v])
        retrain_e |= set(m.get("episodes", [])) | set(m.get("replay", []))
        retrain_p |= set(m.get("pairs", []))
    p.retrain_episodes = sorted(retrain_e - gone_e)
    p.retrain_pairs = sorted(retrain_p - gone_p)
    return p


def stage(conn: sqlite3.Connection, p: LethePlan) -> str:
    """Store the plan and return the confirmation code the user must echo back."""
    p.code = secrets.token_hex(3)
    dbmod.set_meta(conn, "lethe_pending", {**p.as_dict(), "created": time.time()})
    return p.code


def take_pending(conn: sqlite3.Connection, code: str, max_age: float = 900) -> LethePlan | None:
    data = dbmod.get_meta(conn, "lethe_pending")
    if not data or data.get("code") != code or time.time() - float(data.get("created", 0)) > max_age:
        return None
    dbmod.set_meta(conn, "lethe_pending", None)
    data.pop("created", None)
    return LethePlan(**data)


def exclude(conn: sqlite3.Connection, p: LethePlan) -> None:
    """Mark the forgotten data excluded so no future night, exam or dataset uses it."""
    for table, ids in (("episodes", p.episodes), ("pairs", p.pairs), ("habits", p.habits), ("dreams", p.dreams)):
        if ids:
            conn.execute(f"UPDATE {table} SET excluded = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
    if p.habits:
        conn.execute(f"UPDATE habits SET status = 'forgotten' WHERE id IN ({','.join('?' * len(p.habits))})", p.habits)
        conn.execute(f"UPDATE dreams SET excluded = 1 WHERE habit_id IN ({','.join('?' * len(p.habits))})", p.habits)


def retire_contaminated(conn: sqlite3.Connection, p: LethePlan, keep_version: int | None) -> list[int]:
    """Retire every contaminated brain and delete its adapter. Promote the clean parent if needed."""
    gone = []
    for v in p.affected:
        row = conn.execute("SELECT adapter_path, status FROM brains WHERE version = ?", (v,)).fetchone()
        if row is None:
            continue
        if row["adapter_path"]:
            shutil.rmtree(row["adapter_path"], ignore_errors=True)
        conn.execute("UPDATE brains SET status = 'forgotten', adapter_path = NULL WHERE version = ?", (v,))
        gone.append(v)
    current = conn.execute("SELECT version FROM brains WHERE status = 'promoted'").fetchone()
    if current is None and keep_version is None and p.parent:
        conn.execute("UPDATE brains SET status = 'promoted', promoted_at = ? WHERE version = ?", (time.time(), p.parent))
    return gone


# --- diff and export -------------------------------------------------------------------------------------

def _exam(conn, version: int) -> dict:
    row = conn.execute("SELECT exam_json FROM brains WHERE version = ?", (version,)).fetchone()
    return dbmod.loads(row["exam_json"], {}).get("candidate", {}) if row else {}


def diff(conn: sqlite3.Connection, data_dir: Path, a: int, b: int, samples: int = 4) -> str:
    rows = {r["version"]: r for r in conn.execute("SELECT * FROM brains WHERE version IN (?, ?)", (a, b))}
    if a not in rows or b not in rows:
        return f"Need two existing brains (have {', '.join(f'v{v}' for v in sorted(rows)) or 'none'})."
    ea, eb = _exam(conn, a), _exam(conn, b)
    lines = [f"Brain v{a} vs v{b}"]
    for key, label in (("heldout_acc", "held-out next action"), ("probe_pass", "habit probes"),
                       ("dream_pass", "dream verifiers"), ("validity", "valid outputs")):
        va, vb = ea.get(key), eb.get(key)
        if va is None and vb is None:
            continue
        delta = "" if va is None or vb is None else f" ({'+' if vb >= va else ''}{(vb - va) * 100:.0f} pts)"
        lines.append(f"  {label:<22} {'-' if va is None else f'{va:.0%}'} -> {'-' if vb is None else f'{vb:.0%}'}{delta}")

    def consolidated(v):
        return {r[0] for r in conn.execute("SELECT habit_id FROM habit_results WHERE brain_version = ? AND with_text = 0 "
                                           "AND consolidated = 1", (v,))}

    statements = {r["id"]: r["statement"] for r in conn.execute("SELECT id, statement FROM habits")}
    gained, lost = consolidated(b) - consolidated(a), consolidated(a) - consolidated(b)
    for hid in sorted(gained):
        lines.append(f"  + consolidated: {statements.get(hid, hid)}")
    for hid in sorted(lost):
        lines.append(f"  - lost: {statements.get(hid, hid)}")
    shown = 0
    outs = {}
    for v in (a, b):
        night = rows[v]["night_id"]
        path = data_dir / "nights" / str(night) / "candidate_out.jsonl"
        if path.exists():
            outs[v] = {o["id"]: o["text"] for o in map(json.loads, path.read_text().splitlines()) if o}
    if a in outs and b in outs:
        from .brainfmt import parse_output
        exam_items = {}
        exam_path = data_dir / "nights" / str(rows[b]["night_id"]) / "exam.jsonl"
        if exam_path.exists():
            exam_items = {x["id"]: x for x in map(json.loads, exam_path.read_text().splitlines()) if x}
        common = [i for i in outs[b] if i in outs[a]]
        lines.append("  side by side on shared exam items:" if common else "  no shared exam items to compare")
        for item in common:
            pa, pb = parse_output(outs[a][item]), parse_output(outs[b][item])
            if pa == pb:
                continue
            msgs = (exam_items.get(item) or {}).get("messages") or []
            user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), item)

            def show(x):
                return (x.get("args") or {}).get("command") or x.get("text") or x.get("invalid") or "?"

            lines.append(f"    \"{str(user)[:60]}\"")
            lines.append(f"       v{a}: {show(pa)[:70]}")
            lines.append(f"       v{b}: {show(pb)[:70]}")
            shown += 1
            if shown >= samples:
                break
    return "\n".join(lines)


def export(conn: sqlite3.Connection, version: int, dest: Path) -> Path:
    """Copy the adapter with a model card and a data manifest hash. Never exports captured data."""
    row = conn.execute("SELECT * FROM brains WHERE version = ?", (version,)).fetchone()
    if row is None or not row["adapter_path"] or not Path(row["adapter_path"]).exists():
        raise ValueError(f"Brain v{version} has no adapter on disk.")
    out = Path(dest).expanduser() / f"hypnos-brain-v{version}"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(row["adapter_path"], out)
    manifest = _manifest(row)
    exam = dbmod.loads(row["exam_json"], {})
    habits = [r["statement"] for r in conn.execute(
        "SELECT h.statement FROM habit_results r JOIN habits h ON h.id = r.habit_id WHERE r.brain_version = ? "
        "AND r.with_text = 0 AND r.consolidated = 1", (version,))]
    summary = {"version": version, "parent_version": row["parent_version"], "base_model": row["base_model"],
               "manifest_hash": row["manifest_hash"],
               "data_counts": {k: len(manifest.get(k, [])) for k in ("episodes", "pairs", "habits", "replay")},
               "exam": exam.get("candidate", {}), "consolidated_habits": habits, "exported_at": time.time()}
    (out / "hypnos_manifest.json").write_text(json.dumps(summary, indent=2))
    card = [f"# Hypnos brain v{version}", "",
            f"A LoRA adapter for `{row['base_model']}` trained by Hypnos, a Hermes Agent plugin, on one user's own",
            "working habits. It contains no captured conversations; this folder holds only the adapter weights,",
            "this card, and a hash of the data manifest.", "",
            f"- Lineage: parent {('v' + str(row['parent_version'])) if row['parent_version'] else 'base model'}",
            f"- Data manifest SHA-256: `{row['manifest_hash']}`",
            f"- Held-out next-action accuracy: {exam.get('candidate', {}).get('heldout_acc', 0):.0%}",
            f"- Habit probe pass rate: {exam.get('candidate', {}).get('probe_pass', 0):.0%}", "",
            "Habits consolidated into these weights:"] + [f"- {h}" for h in habits or ["(none)"]]
    (out / "README.md").write_text("\n".join(card) + "\n")
    digest = hashlib.sha256()
    for f in sorted(out.rglob("*")):
        if f.is_file():
            digest.update(f.read_bytes())
    (out / "SHA256SUMS").write_text(digest.hexdigest() + "  (whole folder)\n")
    return out
