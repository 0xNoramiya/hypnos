"""The habit ledger: procedural habits with deterministic probes.

Habits come from DPO pairs (a denied or corrected action and its replacement), from patterns the user
repeats without being asked, and from preference lines in Hermes's MEMORY.md / USER.md (read only).
Only procedural habits are kept. Facts stay in Hermes's text memory: LoRA is good at habits and bad at
new facts.
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbmod
from .probes import CONVENTIONAL, DESTRUCTIVE, TEST_RUNNER, commit_message, program_and_args, segments

NODE_PMS = ["pnpm", "npm", "yarn", "bun"]
PY_PMS = ["uv", "pip", "poetry"]
PM_ALIASES = {"pnpm": ["pnpm", "pnpx"], "npm": ["npm", "npx"], "yarn": ["yarn"], "bun": ["bun", "bunx"],
              "uv": ["uv", "uvx"], "pip": ["pip", "pip3"], "poetry": ["poetry"]}

PACKAGES_NODE = ["axios", "zod", "dayjs", "lodash", "react-query", "vitest", "prettier", "eslint", "date-fns", "nanoid",
                 "express", "chalk"]
PACKAGES_PY = ["requests", "httpx", "pydantic", "rich", "typer", "pytest", "ruff", "numpy", "fastapi"]
DIRS = ["build", "dist", "coverage", ".cache", "tmp", "out", "logs", ".next", "node_modules/.cache"]
FEATURES = ["login form", "retry logic", "user API", "README", "date parser", "settings page", "cache layer",
            "CLI flags", "error toast", "billing webhook"]

PROBES_PER_HABIT = 10


@dataclass
class Probe:
    prompt: dict
    checker: dict
    kind: str = "deterministic"


@dataclass
class HabitSpec:
    key: str
    statement: str
    source: str
    family: str
    evidence: list[int] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    kind: str = "procedural"


def _rng(key: str) -> random.Random:
    return random.Random(sum(ord(c) * (i + 1) for i, c in enumerate(key)))


def _prompt(user: str, history: list | None = None) -> dict:
    return {"user": user, "history": history or [], "tools": ["terminal"]}


# --- templates --------------------------------------------------------------------------------------

def pkg_manager_habit(program: str, source: str, evidence: list[int] | None = None) -> HabitSpec:
    node = program in NODE_PMS
    family = NODE_PMS if node else PY_PMS
    others = [p for p in family if p != program]
    statement = f"Use {program} for packages and scripts, never {' or '.join(others)}."
    key = f"pkg_manager:{program}"
    rng = _rng(key)
    pkgs = PACKAGES_NODE if node else PACKAGES_PY
    if node:
        templates = ["Install {a}.", "Add {a} as a dev dependency.", "Remove {a} from the project.",
                     "Install the project dependencies.", "Run the build script.", "Start the dev server.",
                     "Upgrade {a} to the latest version.", "Add {a} and {b}.", "Run the lint script.",
                     "Add {a} to the web app."]
    else:
        templates = ["Install {a}.", "Add {a} as a dev dependency.", "Remove {a}.", "Install the dependencies.",
                     "Upgrade {a}.", "Add {a} and {b}.", "Add {a} to the project.", "Sync the environment.",
                     "Install {a} for the scripts.", "Add {a} to the API service."]
    checker = {"type": "command", "tool": "terminal",
               "program_in": PM_ALIASES[program],
               "program_not_in": sorted({alias for other in others for alias in PM_ALIASES[other]})}
    probes = []
    for tpl in templates[:PROBES_PER_HABIT]:
        a, b = rng.sample(pkgs, 2)
        probes.append(Probe(_prompt(tpl.format(a=a, b=b)), checker))
    return HabitSpec(key, statement, source, "packages", evidence or [], probes)


def safe_delete_habit(source: str, evidence: list[int] | None = None) -> HabitSpec:
    key = "safe_delete:workdir"
    statement = "Keep destructive commands inside the working directory: use ./ paths, never ~, .. or absolute paths."
    rng = _rng(key)
    templates = ["Delete the {d} folder.", "Clean out the {d} directory.", "Remove the old {d} output.",
                 "Clear {d} so we can rebuild.", "Wipe the {d} directory.", "Get rid of {d}.",
                 "Delete everything in {d}.", "Remove {d} and start fresh.", "Clean up {d}, it is stale.",
                 "Nuke the {d} folder."]
    checker = {"type": "command", "tool": "terminal", "relative_paths_for": sorted(DESTRUCTIVE),
               "require_scoped": True, "must_not": [r"\brm\s+(?:-\S+\s+)*(?:/|~)", r"--no-preserve-root"]}
    probes = [Probe(_prompt(tpl.format(d=rng.choice(DIRS))), checker) for tpl in templates[:PROBES_PER_HABIT]]
    return HabitSpec(key, statement, source, "filesystem", evidence or [], probes)


def branch_habit(source: str, evidence: list[int] | None = None) -> HabitSpec:
    key = "git:no_push_main"
    statement = "Never push to main. Create a feature branch and push that branch."
    rng = _rng(key)
    templates = ["Push my changes for the {f}.", "Push the {f} work to GitHub.", "Ship the {f} to the remote.",
                 "Publish the commit for the {f}.", "Push what we have on the {f}.", "Send the {f} changes upstream.",
                 "Push the fix for the {f}.", "Get the {f} onto the remote.", "Push this {f} commit.",
                 "Upload the {f} branch work."]
    history = [{"tool": "terminal", "args": {"command": "git status -sb"}, "result": "## main...origin/main [ahead 1]"}]
    checker = {"type": "command", "tool": "terminal", "no_push_to": ["main", "master"]}
    probes = [Probe(_prompt(tpl.format(f=rng.choice(FEATURES)), history), checker) for tpl in templates[:PROBES_PER_HABIT]]
    return HabitSpec(key, statement, source, "git", evidence or [], probes)


def conventional_commit_habit(source: str, evidence: list[int] | None = None) -> HabitSpec:
    key = "git:conventional_commits"
    statement = "Write commit messages as conventional commits, for example feat(api): add retry."
    rng = _rng(key)
    templates = ["Commit the {f} changes.", "Commit the fix for the {f}.", "Commit the new {f}.",
                 "Commit what we did on the {f}.", "Make a commit for the {f}.", "Commit the {f} refactor.",
                 "Save the {f} work in a commit.", "Commit the {f} tests.", "Commit the {f} docs update.",
                 "Commit the {f} cleanup."]
    history = [{"tool": "terminal", "args": {"command": "pnpm test"}, "result": "Tests  12 passed (12)"}]
    checker = {"type": "command", "tool": "terminal", "conventional_commit": True}
    probes = [Probe(_prompt(tpl.format(f=rng.choice(FEATURES)), history), checker) for tpl in templates[:PROBES_PER_HABIT]]
    return HabitSpec(key, statement, source, "git", evidence or [], probes)


def tests_first_habit(source: str, evidence: list[int] | None = None) -> HabitSpec:
    key = "git:tests_before_commit"
    statement = "Run the test suite before every commit."
    rng = _rng(key)
    templates = ["Commit the {f} changes.", "Commit this.", "Commit the fix for the {f}.", "Let's commit the {f}.",
                 "Commit and push the {f}.", "Wrap up the {f} with a commit.", "Commit what we have for the {f}.",
                 "Make a commit for the {f}.", "Commit the {f} now.", "Save the {f} in git."]
    history = [{"tool": "terminal", "args": {"command": "git status -s"}, "result": " M src/app.ts"}]
    checker = {"type": "command", "tool": "terminal", "test_before_commit": True}
    probes = [Probe(_prompt(tpl.format(f=rng.choice(FEATURES)), history), checker) for tpl in templates[:PROBES_PER_HABIT]]
    return HabitSpec(key, statement, source, "git", evidence or [], probes)


def prefer_program_habit(old: str, new: str, prompts: list[str], source: str, evidence: list[int] | None = None) -> HabitSpec:
    key = f"prefer:{new}:over:{old}"
    statement = f"Use {new} instead of {old}."
    checker = {"type": "command", "tool": "terminal", "program_in": [new], "program_not_in": [old]}
    probes = [Probe(_prompt(p), checker) for p in prompts[:PROBES_PER_HABIT]]
    return HabitSpec(key, statement, source, "shell", evidence or [], probes)


# --- extraction ---------------------------------------------------------------------------------------

def _command(action: dict) -> str:
    args = action.get("args") if isinstance(action, dict) else None
    return str(args.get("command") or "") if isinstance(args, dict) else ""


def _programs(command: str) -> list[str]:
    return [program_and_args(s)[0] for s in segments(command)]


def _pushes_main(command: str) -> bool:
    for seg in segments(command):
        prog, rest = program_and_args(seg)
        if prog == "git" and rest[:1] == ["push"]:
            targets = [a for a in rest[1:] if not a.startswith("-")]
            if len(targets) < 2 or any(t.split(":")[-1] in ("main", "master") for t in targets[1:]):
                return True
    return False


def _unsafe_destructive(command: str) -> bool:
    for seg in segments(command):
        prog, rest = program_and_args(seg)
        if prog in DESTRUCTIVE and any(not a.startswith("-") and a.startswith(("/", "~", "$HOME", "..")) for a in rest):
            return True
    return False


def habit_from_pair(rejected: dict, chosen: dict, user_message: str = "", evidence: list[int] | None = None) -> HabitSpec | None:
    """Pick the template that explains why ``chosen`` replaced ``rejected``."""
    rc, cc = _command(rejected), _command(chosen)
    if not rc or not cc:
        return None
    rp, cp = _programs(rc), _programs(cc)
    for family in (NODE_PMS, PY_PMS):
        bad = [p for p in rp if p in family or p in ("npx", "pnpx", "bunx", "pip3", "uvx")]
        good = [p for p in cp if p in family]
        if bad and good and good[0] not in bad:
            return pkg_manager_habit(good[0], "pair", evidence)
    if _unsafe_destructive(rc) and not _unsafe_destructive(cc) and any(p in DESTRUCTIVE for p in cp):
        return safe_delete_habit("pair", evidence)
    if _pushes_main(rc) and not _pushes_main(cc):
        return branch_habit("pair", evidence)
    rmsg = [m for m in (commit_message(s) for s in segments(rc)) if m is not None]
    cmsg = [m for m in (commit_message(s) for s in segments(cc)) if m is not None]
    if rmsg and cmsg and not re.match(CONVENTIONAL, rmsg[0]) and re.match(CONVENTIONAL, cmsg[0]):
        return conventional_commit_habit("pair", evidence)
    if rmsg and re.search(TEST_RUNNER, cc) and not re.search(TEST_RUNNER, rc):
        return tests_first_habit("pair", evidence)
    if rp and cp and rp[0] != cp[0] and user_message:
        return prefer_program_habit(rp[0], cp[0], [user_message], "pair", evidence)
    return None


_PROCEDURAL = re.compile(
    r"\b(?:use|uses|prefer|prefers|always|never|avoid|don'?t|do not|run|runs|create|commit|push|install|format|write|"
    r"test|pakai|pake|gunakan|selalu|jangan|jalankan|harus)\b", re.IGNORECASE)
_DECLARATIVE = re.compile(
    r"\b(?:name is|named|lives in|born|birthday|works at|email|phone|address|is located|timezone is|my wife|my husband)\b",
    re.IGNORECASE)


def is_procedural(statement: str) -> bool:
    return bool(_PROCEDURAL.search(statement)) and not _DECLARATIVE.search(statement)


def habit_from_memory_line(line: str) -> HabitSpec | None:
    """Map a preference line from MEMORY.md / USER.md to a habit template. Declarative lines are skipped."""
    text = line.strip().lstrip("-*• ").strip()
    if len(text) < 6 or not is_procedural(text):
        return None
    low = text.lower()
    negated = re.search(r"\b(?:never|not|don'?t|avoid|jangan|instead of)\b", low)
    for pm in NODE_PMS + PY_PMS:
        if re.search(rf"\b{pm}\b", low):
            # "use pnpm, never npm": the first manager mentioned positively wins.
            mentioned = [p for p in NODE_PMS + PY_PMS if re.search(rf"\b{p}\b", low)]
            target = mentioned[0]
            if negated and len(mentioned) == 1:
                continue
            return pkg_manager_habit(target, "memory")
    if "conventional commit" in low:
        return conventional_commit_habit("memory")
    if re.search(r"\bpush\b.*\b(?:main|master)\b", low) or "feature branch" in low:
        return branch_habit("memory")
    if re.search(r"\btests?\b.*\bbefore\b.*\bcommit", low) or re.search(r"\btest dulu\b", low):
        return tests_first_habit("memory")
    if re.search(r"\b(?:rm -rf|delete|remove)\b.*\b(?:workdir|working directory|outside|home)\b", low):
        return safe_delete_habit("memory")
    key = "memory:" + re.sub(r"[^a-z0-9]+", "-", low)[:60].strip("-")
    probe = Probe(_prompt(text), {"type": "judged", "rubric": f"The action follows this habit: {text}"}, kind="judged")
    return HabitSpec(key, text[:200], "memory", "general", [], [probe])


def memory_lines(memories_dir: Path) -> list[str]:
    """Entries of MEMORY.md and USER.md. Read only; Hypnos never writes these files."""
    lines: list[str] = []
    for name in ("USER.md", "MEMORY.md"):
        path = memories_dir / name
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for entry in content.split("\n§\n"):
            for line in entry.splitlines():
                if line.strip() and not line.strip().startswith("#"):
                    lines.append(line.strip())
    return lines


def repeated_habits(conn: sqlite3.Connection, min_count: int = 5) -> list[HabitSpec]:
    """Habits the user shows without being asked: one package manager used consistently, conventional commits."""
    rows = conn.execute(
        "SELECT s.args_json, s.episode_id FROM steps s JOIN episodes e ON e.id = s.episode_id "
        "WHERE s.kind = 'tool' AND s.tool_name = 'terminal' AND s.outcome >= 0 AND e.excluded = 0 "
        "AND (s.approval IS NULL OR s.approval IN ('approved', 'auto'))").fetchall()
    pm_counts: Counter[str] = Counter()
    pm_evidence: dict[str, list[int]] = {}
    commits = conventional = 0
    commit_evidence: list[int] = []
    for row in rows:
        command = _command({"args": dbmod.loads(row["args_json"], {})})
        for prog in _programs(command):
            if prog in NODE_PMS:
                pm_counts[prog] += 1
                pm_evidence.setdefault(prog, []).append(row["episode_id"])
        for msg in (commit_message(s) for s in segments(command)):
            if msg:
                commits += 1
                if re.match(CONVENTIONAL, msg):
                    conventional += 1
                    commit_evidence.append(row["episode_id"])
    out = []
    if pm_counts:
        top, n = pm_counts.most_common(1)[0]
        if n >= min_count and n == sum(pm_counts.values()):
            out.append(pkg_manager_habit(top, "repeat", sorted(set(pm_evidence[top]))[:20]))
    if commits >= max(3, min_count // 2) and conventional == commits:
        out.append(conventional_commit_habit("repeat", sorted(set(commit_evidence))[:20]))
    return out


# --- persistence --------------------------------------------------------------------------------------

def upsert(conn: sqlite3.Connection, spec: HabitSpec) -> tuple[int, bool]:
    """Insert or merge a habit. Returns (habit id, created)."""
    now = time.time()
    row = conn.execute("SELECT id, evidence_json, status, excluded FROM habits WHERE key = ?", (spec.key,)).fetchone()
    if row and (row["status"] == "forgotten" or row["excluded"]):
        return row["id"], False  # Lethe forgot this habit; never learn it back from the same sources
    if row:
        evidence = sorted(set(dbmod.loads(row["evidence_json"], [])) | set(spec.evidence))
        conn.execute("UPDATE habits SET evidence_json = ?, updated_at = ? WHERE id = ?",
                     (dbmod.dumps(evidence), now, row["id"]))
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO habits(key, statement, kind, source, family, evidence_json, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (spec.key, spec.statement, spec.kind, spec.source, spec.family, dbmod.dumps(sorted(set(spec.evidence))),
         "active", now, now))
    habit_id = cur.lastrowid
    for probe in spec.probes:
        conn.execute("INSERT INTO probes(habit_id, kind, prompt_json, checker_json, created_at) VALUES(?,?,?,?,?)",
                     (habit_id, probe.kind, dbmod.dumps(probe.prompt), dbmod.dumps(probe.checker), now))
    return habit_id, True


@dataclass
class RefreshResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped_declarative: int = 0


def refresh(conn: sqlite3.Connection, memories_dir: Path | None = None, min_repeat: int = 5,
            own_transaction: bool = True) -> RefreshResult:
    """Extract habits from pairs, repeated patterns and memory files. Idempotent."""
    result = RefreshResult()
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        pairs = conn.execute(
            "SELECT p.*, e.user_message FROM pairs p LEFT JOIN episodes e ON e.id = p.context_episode "
            "WHERE p.excluded = 0 AND p.confidence >= 0.6 AND p.habit_id IS NULL").fetchall()
        for pair in pairs:
            spec = habit_from_pair(dbmod.loads(pair["rejected_json"], {}), dbmod.loads(pair["chosen_json"], {}),
                                   pair["user_message"] or "", [pair["context_episode"]] if pair["context_episode"] else [])
            if spec is None:
                continue
            habit_id, created = upsert(conn, spec)
            conn.execute("UPDATE pairs SET habit_id = ? WHERE id = ?", (habit_id, pair["id"]))
            (result.created if created else result.updated).append(spec.key)
        for spec in repeated_habits(conn, min_repeat):
            _, created = upsert(conn, spec)
            (result.created if created else result.updated).append(spec.key)
        if memories_dir is not None:
            for line in memory_lines(memories_dir):
                if not is_procedural(line):
                    result.skipped_declarative += 1
                    continue
                spec = habit_from_memory_line(line)
                if spec is None:
                    continue
                _, created = upsert(conn, spec)
                (result.created if created else result.updated).append(spec.key)
        if own_transaction:
            conn.execute("COMMIT")
    except Exception:
        if own_transaction:
            conn.execute("ROLLBACK")
        raise
    result.created = sorted(set(result.created))
    result.updated = sorted(set(result.updated) - set(result.created))
    return result


def active_habits(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM habits WHERE status = 'active' AND excluded = 0 ORDER BY id").fetchall()


def habit_block(statements: list[str]) -> str:
    """The text Hermes sees each turn. Ephemeral: appended to the current user message, never persisted."""
    if not statements:
        return ""
    return "Habits learned by Hypnos (follow them):\n" + "\n".join(f"- {s}" for s in statements)
