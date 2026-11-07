"""Detect when the user's next message corrects or re-asks the previous turn.

Deterministic patterns in English and Indonesian come first. They run off the hook thread, in the
capture writer. An optional teacher classifier can refine borderline cases at night.

A correction is a negative signal on the *previous* turn, and when the new turn produces a better
action of the same kind, the two form a DPO pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (pattern, weight). Weight is how sure we are that a match means "you got it wrong".
_STRONG = 0.9
_MEDIUM = 0.7
_WEAK = 0.45

_EN: list[tuple[str, float]] = [
    (r"^\s*(?:no|nope|nah)\b[\s,.!:;-]", _STRONG),
    (r"^\s*(?:no|nope)\s*$", _STRONG),
    (r"\b(?:i|we)\s+(?:meant|mean|said|asked(?: for)?|wanted)\b", _STRONG),
    (r"\bnot\s+what\s+(?:i|we)\s+(?:meant|asked|wanted|said)\b", _STRONG),
    (r"\bthat'?s\s+(?:wrong|incorrect|not\s+right|not\s+it)\b", _STRONG),
    (r"\b(?:wrong|incorrect)\s+(?:command|file|branch|one|way|approach|tool)\b", _STRONG),
    (r"\b(?:don'?t|do\s+not|never|stop)\s+(?:use|run|do|push|commit|delete|touch|call|install)\b", _MEDIUM),
    (r"\buse\s+\S+(?:\s+\S+)?\s+instead\b", _STRONG),
    (r"\binstead\s+of\b", _MEDIUM),
    (r"\binstead\b", _WEAK),
    (r"^\s*actually\b", _MEDIUM),
    (r"\bshould\s+(?:have\s+)?(?:be|use|run|been)\b", _WEAK),
    (r"\bwhy\s+did\s+you\b", _MEDIUM),
    (r"\b(?:undo|revert|roll\s*back)\s+(?:that|this|it)\b", _STRONG),
    (r"\btry\s+again\b", _MEDIUM),
    (r"\bplease\s+(?:re-?do|fix)\s+(?:that|this|it)\b", _MEDIUM),
    (r"\byou\s+(?:forgot|missed|ignored|broke)\b", _STRONG),
    (r"\bnot\s+\S+(?:\s+\S+)?\s*,\s*(?:use|but)\b", _MEDIUM),
    (r"\bwe\s+(?:always|never|don'?t)\b", _MEDIUM),
    (r"^\s*(?:please\s+)?use\s+(?:a|an|the)?\s*\S+", _MEDIUM),
    (r"\bonly\s+(?:delete|remove|touch|change|edit|run|use|push)\b", _MEDIUM),
    (r"\bi\s+(?:always|never)\s+(?:use|run|want)\b", _MEDIUM),
]

_ID: list[tuple[str, float]] = [
    (r"^\s*(?:bukan|enggak|nggak|ngga|gak|ga|tidak)\b", _STRONG),
    (r"\bbukan\s+(?:itu|ini|begitu|gitu|yang\s+itu|maksud)\b", _STRONG),
    (r"\bmaksud(?:ku|nya|\s+saya|\s+aku|\s+gue)?\b", _MEDIUM),
    (r"\b(?:salah|keliru)\b", _STRONG),
    (r"\bjangan\b", _MEDIUM),
    (r"\b(?:pakai|pake|gunakan|gunain)\s+\S+(?:\s+\S+)?\s+(?:aja|saja)\b", _MEDIUM),
    (r"\b(?:harusnya|seharusnya|mestinya)\b", _MEDIUM),
    (r"\bsudah\s+(?:aku|saya|gue)\s+bilang\b", _STRONG),
    (r"\b(?:ulang(?:i)?|coba\s+lagi)\b", _MEDIUM),
    (r"\bbatal(?:kan)?\b", _MEDIUM),
    (r"\bkan\s+(?:aku|saya|gue)\s+(?:bilang|minta)\b", _STRONG),
    (r"\bselalu\s+(?:pakai|pake|gunakan)\b", _MEDIUM),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), w, "en") for p, w in _EN] + [
    (re.compile(p, re.IGNORECASE), w, "id") for p, w in _ID]

# Phrases that look like corrections but are not ("no problem", "no worries", "tidak apa-apa").
_NEGATORS = re.compile(
    r"^\s*(?:no\s+(?:problem|worries|thanks|need)|nope,?\s+(?:that'?s\s+)?(?:fine|all|good)"
    r"|tidak\s+apa|gak\s+apa|ga\s+apa|nggak\s+apa|enggak\s+apa)", re.IGNORECASE)

_WORD = re.compile(r"[a-z0-9_./@-]+", re.IGNORECASE)


@dataclass
class Verdict:
    is_correction: bool = False
    is_reask: bool = False
    confidence: float = 0.0
    language: str = ""
    matched: list[str] = field(default_factory=list)

    @property
    def negative(self) -> bool:
        return self.is_correction or self.is_reask


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text or "") if len(t) > 2}


def similarity(a: str, b: str) -> float:
    """Token Jaccard similarity, used for re-ask detection."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def detect(message: str, previous_user_message: str = "") -> Verdict:
    """Classify ``message`` as a correction and/or re-ask of the previous turn."""
    verdict = Verdict()
    text = (message or "").strip()
    if not text:
        return verdict
    head = text[:600]
    if _NEGATORS.search(head):
        return verdict
    best = 0.0
    hits = 0
    for pattern, weight, lang in _COMPILED:
        match = pattern.search(head)
        if match:
            hits += 1
            verdict.matched.append(match.group(0).strip())
            if weight > best:
                best = weight
                verdict.language = lang
    if hits:
        # Several independent cues push confidence up, capped below certainty.
        confidence = min(0.97, best + 0.05 * (hits - 1))
        verdict.confidence = confidence
        verdict.is_correction = confidence >= 0.5
    if previous_user_message:
        sim = similarity(text, previous_user_message)
        if sim >= 0.6 and len(_tokens(text)) >= 3:
            verdict.is_reask = True
            verdict.confidence = max(verdict.confidence, min(0.8, sim))
            verdict.matched.append(f"reask:{sim:.2f}")
    return verdict
