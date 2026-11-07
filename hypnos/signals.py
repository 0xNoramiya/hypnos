"""Signal kinds, their weights, sleep pressure, and action similarity for pair matching."""

from __future__ import annotations

import math
import re
import shlex
import sqlite3
from dataclasses import dataclass

# kind -> (polarity, weight). Corrections and denials weigh most: they are what a night can fix.
SIGNALS: dict[str, tuple[int, float]] = {
    "approval_denied": (-1, 3.0),
    "smart_denied": (-1, 1.0),
    "correction": (-1, 3.0),
    "reask": (-1, 2.0),
    "mark_bad": (-1, 3.0),
    "test_fail": (-1, 1.0),
    "tool_fail": (-1, 0.5),
    "approval_granted": (1, 0.5),
    "mark_good": (1, 1.0),
    "test_pass": (1, 0.5),
    "tool_pass": (1, 0.1),
    "gold_pair": (1, 4.0),
    "pair": (1, 2.0),
}

APPROVED_CHOICES = {"once", "session", "always"}
DENIED_CHOICES = {"deny"}
SMART_DENIED = {"smart_deny"}


def signal_weight(kind: str, confidence: float = 1.0) -> tuple[int, float]:
    polarity, weight = SIGNALS.get(kind, (0, 0.0))
    return polarity, round(weight * max(0.0, min(1.0, confidence)), 4)


def pressure_from_sum(total: float, scale: float = 25.0) -> float:
    """Map the weighted signal sum to 0..100. ``scale`` is the sum that reads as 63%."""
    if total <= 0:
        return 0.0
    scale = max(1.0, float(scale))
    return round(100.0 * (1.0 - math.exp(-total / scale)), 1)


@dataclass
class Pressure:
    percent: float
    total_weight: float
    counts: dict[str, int]


def sleep_pressure(conn: sqlite3.Connection, scale: float = 25.0) -> Pressure:
    """Weighted sum of signals not yet consolidated by a night."""
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n, COALESCE(SUM(weight), 0) AS w FROM signals "
        "WHERE consolidated_night IS NULL GROUP BY kind").fetchall()
    counts = {row["kind"]: int(row["n"]) for row in rows}
    total = float(sum(row["w"] for row in rows))
    return Pressure(pressure_from_sum(total, scale), round(total, 2), counts)


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"sudo", "env", "command", "time", "nohup", "exec"}


def command_of(action: dict) -> str:
    args = action.get("args") or {}
    if isinstance(args, dict):
        return str(args.get("command") or args.get("cmd") or "")
    return ""


def program_of(command: str) -> str:
    """First real program of a shell command: skips env assignments and wrappers like sudo."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    for word in words:
        if _ENV_ASSIGN.match(word) or word in _WRAPPERS or word.startswith("-"):
            continue
        return word.rsplit("/", 1)[-1]
    return ""


def subcommand_of(command: str) -> str:
    """Program plus its first non-flag argument, e.g. ``git push`` or ``pnpm add``."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    prog = program_of(command)
    seen = False
    for word in words:
        if not seen:
            if word.rsplit("/", 1)[-1] == prog:
                seen = True
            continue
        if not word.startswith("-"):
            return f"{prog} {word}"
    return prog


_TOKEN = re.compile(r"[A-Za-z0-9_.@/-]+")


def action_similarity(a: dict, b: dict) -> float:
    """How likely two actions express the same intent (0..1)."""
    if a.get("tool") != b.get("tool"):
        return 0.0
    ca, cb = command_of(a), command_of(b)
    if not ca and not cb:
        return 0.6
    ta = {t for t in _TOKEN.findall(ca) if len(t) > 1}
    tb = {t for t in _TOKEN.findall(cb) if len(t) > 1}
    jaccard = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    score = jaccard
    if program_of(ca) and program_of(ca) == program_of(cb):
        score = max(score, 0.5)
    return round(min(1.0, score), 3)


# Programs that are different spellings of one intent (npm vs pnpm, rm vs trash).
EQUIVALENT_PROGRAMS = [
    {"npm", "pnpm", "yarn", "bun", "npx", "pnpx"},
    {"pip", "pip3", "uv", "poetry", "pipx"},
    {"rm", "trash", "trash-put", "gio"},
    {"python", "python3", "uv"},
]


def same_intent_family(a: dict, b: dict) -> bool:
    pa, pb = program_of(command_of(a)), program_of(command_of(b))
    if not pa or not pb:
        return False
    if pa == pb:
        return True
    return any(pa in group and pb in group for group in EQUIVALENT_PROGRAMS)


def pair_confidence(rejected: dict, chosen: dict, *, source: str, correction_conf: float = 0.0,
                    explicit_approval: bool = False, chosen_outcome: int = 0, turn_gap: int = 1) -> float:
    """Confidence that ``chosen`` is the corrected version of ``rejected``. Documented in README."""
    sim = action_similarity(rejected, chosen)
    family = same_intent_family(rejected, chosen)
    if sim < 0.2 and not family:
        # A different command can still be the fix when the user said so plainly ("run the tests first").
        if source != "correction" or correction_conf < 0.7 or rejected.get("tool") != chosen.get("tool"):
            return 0.0
    base = 0.55 if source == "denial" else 0.45
    score = base
    score += 0.25 * correction_conf
    score += 0.1 if family else 0.0
    score += 0.1 if explicit_approval else 0.0
    score += 0.05 if chosen_outcome > 0 else (-0.15 if chosen_outcome < 0 else 0.0)
    score -= 0.05 * max(0, turn_gap - 1)
    if command_of(rejected) and command_of(rejected) == command_of(chosen):
        return 0.0
    return round(max(0.0, min(0.97, score)), 3)


GOLD_THRESHOLD = 0.75
