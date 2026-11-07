"""The waking exam: grade a candidate brain against its baseline and decide promotion.

Primary metric: the pooled pass rate over held-out next-actions and deterministic habit probes, where
every item is one Bernoulli trial. The candidate is promoted only when

1. the Wilson 95% lower bound of its primary pass rate is above the baseline's point estimate
   (baseline = the current promoted brain, or the untrained base model for the first brain), and
2. it is non-inferior on each component: held-out accuracy, probe pass rate and output validity may not
   drop by more than ``NON_INFERIORITY_MARGIN``, and validity must stay above ``VALIDITY_FLOOR``.

Otherwise it was a bad night: the previous brain stays and the report says why.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .brainfmt import action_from_target, actions_equivalent, parse_output, validate_action
from .probes import check

NON_INFERIORITY_MARGIN = 0.05
VALIDITY_FLOOR = 0.9
CONSOLIDATION_THRESHOLD = 0.9
FAMILY_MIN_N = 2
FAMILY_MIN_ACC = 0.6


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


@dataclass
class ItemResult:
    id: str
    kind: str  # heldout | probe | dream
    passed: bool
    valid: bool
    family: str = ""
    habit_id: int | None = None
    reason: str = ""
    mean_logprob: float = 0.0
    output: dict = field(default_factory=dict)


@dataclass
class Scores:
    heldout: tuple[int, int] = (0, 0)
    probes: tuple[int, int] = (0, 0)
    dreams: tuple[int, int] = (0, 0)
    valid: tuple[int, int] = (0, 0)
    families: dict[str, tuple[int, int]] = field(default_factory=dict)
    habits: dict[int, tuple[int, int]] = field(default_factory=dict)
    mean_logprob: float = 0.0

    @staticmethod
    def rate(pair: tuple[int, int]) -> float:
        return pair[0] / pair[1] if pair[1] else 0.0

    @property
    def primary(self) -> tuple[int, int]:
        return (self.heldout[0] + self.probes[0] + self.dreams[0], self.heldout[1] + self.probes[1] + self.dreams[1])

    def as_dict(self) -> dict:
        return {
            "heldout_acc": round(self.rate(self.heldout), 4), "heldout_n": self.heldout[1],
            "probe_pass": round(self.rate(self.probes), 4), "probe_n": self.probes[1],
            "dream_pass": round(self.rate(self.dreams), 4), "dream_n": self.dreams[1],
            "validity": round(self.rate(self.valid), 4),
            "primary": round(self.rate(self.primary), 4), "primary_n": self.primary[1],
            "primary_lower": round(wilson_lower(*self.primary), 4),
            "families": {f: {"acc": round(self.rate(v), 4), "n": v[1]} for f, v in sorted(self.families.items())},
            "habits": {str(h): {"pass": v[0], "total": v[1]} for h, v in sorted(self.habits.items())},
            "mean_logprob": round(self.mean_logprob, 4),
        }


def grade(items: list[dict], outputs: list[dict]) -> tuple[Scores, list[ItemResult]]:
    """``items`` carry the expected answer (``target`` or ``checker``); ``outputs`` are worker generations."""
    by_id = {o.get("id"): o for o in outputs}
    scores = Scores()
    results: list[ItemResult] = []
    fam: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    hab: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    lps = []
    counts = {"heldout": [0, 0], "probe": [0, 0], "dream": [0, 0], "valid": [0, 0]}
    for item in items:
        out = by_id.get(item["id"]) or {"text": "", "mean_logprob": -99.0}
        action = parse_output(out.get("text", ""))
        reason = validate_action(action, item.get("tools") or [])
        valid = not reason
        kind = item.get("kind", "heldout")
        if kind == "heldout":
            passed = valid and actions_equivalent(action, action_from_target(item["target"]))
            if valid and not passed:
                reason = "different action"
        else:
            verdict = check(item["checker"], action) if valid else None
            passed = bool(verdict and verdict.passed)
            if verdict is not None and not passed:
                reason = verdict.reason
            if verdict is not None and verdict.passed is None:
                continue  # judged probes are graded by the teacher, not here
        counts[kind][0] += int(passed)
        counts[kind][1] += 1
        counts["valid"][0] += int(valid)
        counts["valid"][1] += 1
        if kind == "heldout":
            fam[item.get("family", "")][0] += int(passed)
            fam[item.get("family", "")][1] += 1
        if item.get("habit_id") is not None:
            hab[item["habit_id"]][0] += int(passed)
            hab[item["habit_id"]][1] += 1
        lps.append(float(out.get("mean_logprob", -99.0)))
        results.append(ItemResult(item["id"], kind, passed, valid, item.get("family", ""), item.get("habit_id"),
                                  reason, float(out.get("mean_logprob", -99.0)), action))
    scores.heldout = tuple(counts["heldout"])
    scores.probes = tuple(counts["probe"])
    scores.dreams = tuple(counts["dream"])
    scores.valid = tuple(counts["valid"])
    scores.families = {k: tuple(v) for k, v in fam.items()}
    scores.habits = {k: tuple(v) for k, v in hab.items()}
    scores.mean_logprob = sum(lps) / len(lps) if lps else 0.0
    return scores, results


@dataclass
class Decision:
    promote: bool
    reasons: list[str]
    families: list[str]
    consolidated_habits: list[int]


def decide(candidate: Scores, baseline: Scores, *, threshold: float = CONSOLIDATION_THRESHOLD,
           habit_families: dict[int, str] | None = None) -> Decision:
    reasons = []
    cand_lower = wilson_lower(*candidate.primary)
    base_rate = Scores.rate(baseline.primary)
    better = cand_lower > base_rate
    if not candidate.primary[1]:
        reasons.append("no exam items: capture more turns before sleeping")
        better = False
    elif not better:
        reasons.append(f"primary lower bound {cand_lower:.2f} is not above baseline {base_rate:.2f}")
    checks = [
        ("held-out accuracy", candidate.heldout, baseline.heldout),
        ("habit probes", candidate.probes, baseline.probes),
        ("dream verifiers", candidate.dreams, baseline.dreams),
    ]
    non_inferior = True
    for label, cand, base in checks:
        if cand[1] and base[1] and Scores.rate(cand) < Scores.rate(base) - NON_INFERIORITY_MARGIN:
            non_inferior = False
            reasons.append(f"{label} dropped from {Scores.rate(base):.2f} to {Scores.rate(cand):.2f}")
    if candidate.valid[1] and Scores.rate(candidate.valid) < VALIDITY_FLOOR:
        non_inferior = False
        reasons.append(f"only {Scores.rate(candidate.valid):.0%} of outputs were valid tool calls or answers")
    promote = better and non_inferior
    consolidated = [h for h, (p, t) in candidate.habits.items() if t and p / t >= threshold]
    families = []
    if promote:
        for fam_name, (p, t) in candidate.families.items():
            if t >= FAMILY_MIN_N and p / t >= FAMILY_MIN_ACC:
                families.append(fam_name)
        # A family with too little held-out data is still promotable when its habits are consolidated
        # and nothing it was examined on failed badly.
        for habit_id in consolidated:
            fam_name = (habit_families or {}).get(habit_id)
            if not fam_name or fam_name in families:
                continue
            p, t = candidate.families.get(fam_name, (0, 0))
            if t < FAMILY_MIN_N or p / t >= FAMILY_MIN_ACC:
                families.append(fam_name)
        reasons.insert(0, f"primary lower bound {cand_lower:.2f} beats baseline {base_rate:.2f}")
    return Decision(promote, reasons, sorted(families), sorted(consolidated))
