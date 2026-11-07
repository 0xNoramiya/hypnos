"""Redact secrets and personal data before anything is persisted.

Hermes's own redactor (``agent.redact``) is not part of the public plugin API, so Hypnos ships its
own. The scrubber is deliberately greedy: losing a token of training signal is cheap, leaking a key
into a dataset or a LoRA adapter is not.

Every replacement is a stable placeholder such as ``[REDACTED:aws_key]`` so training examples keep
their shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_PLACEHOLDER = "[REDACTED:{}]"

# Order matters: specific shapes first, broad ones last.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    ("bearer", re.compile(r"(?i)(?<=\bbearer\s)[A-Za-z0-9._~+/=-]{8,}")),
    ("basic_auth", re.compile(r"(?i)(?<=\bbasic\s)[A-Za-z0-9+/=]{12,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprse]-[A-Za-z0-9-]{10,}")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("pypi_token", re.compile(r"\bpypi-[A-Za-z0-9_-]{40,}")),
    ("telegram_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("discord_token", re.compile(r"\b[MNO][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,}")),
    ("runpod_key", re.compile(r"\brpa_[A-Za-z0-9]{20,}")),
    # key=value / key: value / --key value where the key name says "secret".
    ("assigned_secret", re.compile(
        r"(?ix)"
        r"(?<![A-Za-z0-9])"
        r"(?:[A-Za-z0-9_.-]*?(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|passwd|auth[_-]?key"
        r"|access[_-]?key|private[_-]?key|client[_-]?secret|credentials?|cookie))"
        r"(?:\"|')?"
        r"(?:\s*[:=]\s*|\s+(?=[\"']))"
        r"(?:\"|')?"
        r"(?P<value>[^\s\"',;&|]{6,})")),
    ("cli_secret_flag", re.compile(
        r"(?i)(?<=\s)--?(?:password|passwd|token|api-key|apikey|secret|auth-token|access-key)(?:=|\s+)"
        r"(?P<value>[^\s\"']{4,})")),
    ("email", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("ipv6", re.compile(
        r"(?<![:\w])(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}(?![:\w])"
        r"|(?<![:\w])(?:[0-9a-fA-F]{1,4}:){1,6}:(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}(?![:\w])")),
    ("ipv4", re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")),
    ("hex_secret", re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])")),
    ("high_entropy", re.compile(r"(?<![A-Za-z0-9/_.+-])[A-Za-z0-9+_=-]{32,}(?![A-Za-z0-9/_.+=-])")),
]

# IPs that identify nothing.
_SAFE_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "::1"}

_VALUE_GROUP_KINDS = {"assigned_secret", "cli_secret_flag"}


def _entropy_ok(token: str) -> bool:
    """True when a long token looks like a credential rather than a word, path or identifier."""
    classes = sum((
        any(c.islower() for c in token),
        any(c.isupper() for c in token),
        any(c.isdigit() for c in token),
    ))
    if classes < 3:
        return False
    # Long runs of one character class (snake_case identifiers, words) are not secrets.
    digits = sum(c.isdigit() for c in token)
    return digits >= 4 and len(set(token)) >= 16


def _replace(kind: str) -> Callable[[re.Match[str]], str]:
    placeholder = _PLACEHOLDER.format(kind)

    def repl(match: re.Match[str]) -> str:
        text = match.group(0)
        if text.startswith("[REDACTED:"):
            return text
        if kind in _VALUE_GROUP_KINDS:
            value = match.group("value")
            if value.startswith(("[REDACTED:", "$", "<")):
                return text
            start = match.start("value") - match.start(0)
            return text[:start] + placeholder + text[start + len(value):]
        if kind in ("ipv4", "ipv6") and text in _SAFE_IPS:
            return text
        if kind == "ipv4" and text.startswith("127."):
            return text
        if kind == "high_entropy" and not _entropy_ok(text):
            return text
        if kind == "email" and text.lower().startswith("git@"):
            return text
        return placeholder

    return repl


_REPLACERS = [(kind, pattern, _replace(kind)) for kind, pattern in _PATTERNS]


def scrub_text(text: str, extra_literals: tuple[str, ...] = ()) -> str:
    """Return ``text`` with secrets and personal data replaced by placeholders."""
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    for literal in extra_literals:
        if literal and len(literal) >= 6:
            text = text.replace(literal, _PLACEHOLDER.format("known_secret"))
    for _kind, pattern, repl in _REPLACERS:
        text = pattern.sub(repl, text)
    return text


def scrub(value: Any, extra_literals: tuple[str, ...] = (), _depth: int = 0) -> Any:
    """Scrub strings anywhere inside a JSON-like structure. Dict keys are kept as-is."""
    if _depth > 32:
        return "[REDACTED:too_deep]"
    if isinstance(value, str):
        return scrub_text(value, extra_literals)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.search(key) and isinstance(item, str) and item:
                out[key] = _PLACEHOLDER.format("secret_field")
            else:
                out[key] = scrub(item, extra_literals, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item, extra_literals, _depth + 1) for item in value]
    return value


_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|authorization|cookie|private[_-]?key"
    r"|client[_-]?secret|access[_-]?key|credentials?)$")


_SECRET_KINDS = tuple(kind for kind, _ in _PATTERNS if kind not in ("email", "ipv4", "ipv6"))


def contains_secret(text: str) -> bool:
    """True when ``text`` still holds something that looks like a credential.

    Used by the night's N1 pass to verify that persisted data really is scrubbed.
    """
    if not text:
        return False
    scrubbed = scrub_text(text)
    return any(f"[REDACTED:{kind}]" in scrubbed and f"[REDACTED:{kind}]" not in text for kind in _SECRET_KINDS)
