"""Turn captured episodes and pairs into training and exam data, and record exactly what was used.

Every brain stores a manifest listing the episode, pair and dream ids it trained on. That makes lineage
exact: to forget something, retrain from the parent of the first brain that saw it, without it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from . import brainfmt
from . import db as dbmod

HELDOUT_FRACTION = 0.2
RESULT_SNIPPET = 600


def _stable_frac(key: str) -> float:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def assign_families(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id, user_message FROM episodes WHERE family IS NULL").fetchall()
    for row in rows:
        conn.execute("UPDATE episodes SET family = ? WHERE id = ?", (brainfmt.classify(row["user_message"] or ""), row["id"]))
    return len(rows)


def assign_splits(conn: sqlite3.Connection, fraction: float = HELDOUT_FRACTION) -> dict[str, int]:
    """Time-based held-out split per family. Once assigned, a split never changes."""
    rows = conn.execute(
        "SELECT id, family FROM episodes WHERE split IS NULL AND status = 'ok' AND excluded = 0 "
        "ORDER BY started_at, id").fetchall()
    by_family: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        by_family[row["family"] or "chat"].append(row["id"])
    counts = {"train": 0, "heldout": 0}
    for ids in by_family.values():
        n_held = int(round(len(ids) * fraction)) if len(ids) >= 4 else 0
        cut = len(ids) - n_held
        for i, episode_id in enumerate(ids):
            split = "heldout" if i >= cut else "train"
            conn.execute("UPDATE episodes SET split = ? WHERE id = ?", (split, episode_id))
            counts[split] += 1
    return counts


def _steps(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM steps WHERE episode_id = ? ORDER BY step_index", (episode_id,)).fetchall()


def step_action(step: sqlite3.Row) -> dict:
    if step["kind"] == "text":
        return {"text": step["text"] or ""}
    return {"tool": step["tool_name"], "args": dbmod.loads(step["args_json"], {})}


def step_history(step: sqlite3.Row) -> dict:
    entry = step_action(step)
    if step["kind"] != "text":
        entry["result"] = (step["result_text"] or "")[:RESULT_SNIPPET]
    return entry


def _good_step(step: sqlite3.Row) -> bool:
    if step["kind"] == "text":
        return bool((step["text"] or "").strip())
    if step["approval"] in ("denied", "smart_denied"):
        return False
    return step["result_text"] is not None and step["outcome"] >= 0


def _corrected_episodes(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT DISTINCT correction_of FROM episodes WHERE correction_of IS NOT NULL").fetchall()
    bad = {r[0] for r in rows}
    marked = conn.execute("SELECT DISTINCT episode_id FROM signals WHERE kind = 'mark_bad'").fetchall()
    return bad | {r[0] for r in marked if r[0] is not None}


def _toolset(conn: sqlite3.Connection, toolset_hash: str | None) -> list[dict] | None:
    if not toolset_hash:
        return None
    row = conn.execute("SELECT tools_json FROM toolsets WHERE hash = ?", (toolset_hash,)).fetchone()
    return dbmod.loads(row["tools_json"], None) if row else None


def latest_offered(conn: sqlite3.Connection) -> list[dict] | None:
    """The tool schemas Hermes offered most recently, so exam and dream prompts match what serving sees."""
    row = conn.execute("SELECT tools_json FROM toolsets ORDER BY created_at DESC LIMIT 1").fetchone()
    return dbmod.loads(row["tools_json"], None) if row else None


@dataclass
class Example:
    id: str
    family: str
    user: str
    history: list[dict]
    target: dict
    tools: list[dict]
    source: str
    episode_id: int | None = None

    def render(self, habits: list[str]) -> dict:
        return {"id": self.id, "family": self.family, "source": self.source,
                "messages": brainfmt.build_messages(self.user, self.history, habits),
                "tools": self.tools, "target": self.target}


def episode_examples(conn: sqlite3.Connection, split: str, since: float = 0.0,
                     episode_ids: list[int] | None = None) -> list[Example]:
    """Next-action examples: for every good step, predict it from the user message and the steps before it."""
    corrected = _corrected_episodes(conn)
    query = ("SELECT * FROM episodes WHERE split = ? AND status = 'ok' AND excluded = 0 AND started_at >= ? "
             "ORDER BY started_at, id")
    episodes = conn.execute(query, (split, since)).fetchall()
    if episode_ids is not None:
        wanted = set(episode_ids)
        episodes = [e for e in episodes if e["id"] in wanted]
    out: list[Example] = []
    for ep in episodes:
        if ep["id"] in corrected:
            continue
        steps = _steps(conn, ep["id"])
        offered = _toolset(conn, ep["toolset_hash"])
        family = ep["family"] or brainfmt.classify(ep["user_message"] or "")
        tools = brainfmt.brain_tools(offered, family)
        history: list[dict] = []
        for step in steps:
            if not _good_step(step):
                break
            action = step_action(step)
            if action.get("tool") and action["tool"] not in {t["function"]["name"] for t in tools}:
                break
            out.append(Example(f"ep:{ep['id']}:{step['step_index']}", family, ep["user_message"] or "",
                               list(history), brainfmt.target_from_action(action), tools, "episode", ep["id"]))
            history.append(step_history(step))
    return out


@dataclass
class PairExample:
    id: str
    family: str
    user: str
    history: list[dict]
    chosen: dict
    rejected: dict
    tools: list[dict]
    source: str
    gold: bool

    def render(self, habits: list[str]) -> dict:
        return {"id": self.id, "family": self.family, "source": self.source, "gold": self.gold,
                "messages": brainfmt.build_messages(self.user, self.history, habits),
                "tools": self.tools, "chosen": self.chosen, "rejected": self.rejected}

    def as_sft(self) -> Example:
        return Example(self.id + ":chosen", self.family, self.user, self.history, self.chosen, self.tools, self.source)


def pair_examples(conn: sqlite3.Connection, pair_ids: list[int] | None = None, min_confidence: float = 0.6) -> list[PairExample]:
    rows = conn.execute(
        "SELECT p.*, e.user_message, e.family, e.toolset_hash FROM pairs p "
        "LEFT JOIN episodes e ON e.id = p.context_episode WHERE p.excluded = 0 AND p.confidence >= ? ORDER BY p.id",
        (min_confidence,)).fetchall()
    if pair_ids is not None:
        wanted = set(pair_ids)
        rows = [r for r in rows if r["id"] in wanted]
    out = []
    for row in rows:
        detail = dbmod.loads(row["detail"], {}) if (row["detail"] or "").startswith("{") else {}
        user = row["user_message"] or detail.get("user", "")
        family = row["family"] or brainfmt.classify(user)
        history: list[dict] = []
        if row["context_episode"]:
            for step in _steps(conn, row["context_episode"]):
                if step["step_index"] >= (row["context_step"] or 0):
                    break
                history.append(step_history(step))
        elif detail.get("history"):
            history = detail["history"]
        tools = brainfmt.brain_tools(_toolset(conn, row["toolset_hash"]), family)
        chosen = dbmod.loads(row["chosen_json"], {})
        rejected = dbmod.loads(row["rejected_json"], {})
        out.append(PairExample(f"pair:{row['id']}", family, user, history, brainfmt.target_from_action(chosen),
                               brainfmt.target_from_action(rejected), tools, row["source"], bool(row["gold"])))
    return out


@dataclass
class Manifest:
    base_model: str
    parent_version: int | None
    episodes: list[int] = field(default_factory=list)
    pairs: list[int] = field(default_factory=list)
    habits: list[int] = field(default_factory=list)
    replay: list[int] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"base_model": self.base_model, "parent_version": self.parent_version,
                "episodes": sorted(set(self.episodes)), "pairs": sorted(set(self.pairs)),
                "habits": sorted(set(self.habits)), "replay": sorted(set(self.replay)), "params": self.params}

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()


def habit_text_mask(example_id: str, share: float = 0.5) -> bool:
    """Deterministically decide whether an example carries the habit text (context distillation)."""
    return _stable_frac(example_id) < share


def write_jsonl(path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
