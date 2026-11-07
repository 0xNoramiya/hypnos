"""Deterministic habit probes.

A probe is a prompt (user message, optional prior steps, offered tools) plus a checker over the single
next action the model produces. Checkers parse the shell command; they never execute it. Only when no
deterministic check exists is a probe ``judged`` (the teacher grades it at night), and it is marked so.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .signals import _ENV_ASSIGN, _WRAPPERS

TEST_RUNNER = (r"(?:\b(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?test\b|\bpytest\b|\bpython3?\s+-m\s+pytest\b|\bcargo\s+test\b"
               r"|\bgo\s+test\b|\bvitest\b|\bjest\b|\bforge\s+test\b|\bmake\s+test\b|\btox\b|\bnox\b)")
CONVENTIONAL = r"^(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\([\w./-]+\))?!?: \S.*"
DESTRUCTIVE = {"rm", "rmdir", "mv", "chmod", "chown", "trash", "trash-put", "shred", "truncate"}



@dataclass
class CheckResult:
    passed: bool | None  # None means "needs a judge"
    reason: str = ""


def segments(command: str) -> list[str]:
    """Split a shell command line into simple commands. Quotes are respected well enough for checks."""
    parts, buf, quote = [], [], ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif command.startswith(("&&", "||"), i):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        elif ch in ";|\n":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def program_and_args(segment: str) -> tuple[str, list[str]]:
    toks = words(segment)
    while toks and (_ENV_ASSIGN.match(toks[0]) or toks[0] in _WRAPPERS):
        toks = toks[1:]
    if not toks:
        return "", []
    return toks[0].rsplit("/", 1)[-1], toks[1:]


def commit_message(segment: str) -> str | None:
    prog, args = program_and_args(segment)
    if prog != "git" or "commit" not in args:
        return None
    for i, arg in enumerate(args):
        if arg in ("-m", "--message") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--message="):
            return arg.split("=", 1)[1]
        if arg.startswith("-") and not arg.startswith("--") and "m" in arg[1:]:
            # Combined short flags: -am "msg", or -m"msg" / -amMSG with the message attached.
            after = arg[arg.index("m", 1) + 1:]
            if after:
                return after
            if i + 1 < len(args):
                return args[i + 1]
    return ""


def _is_unsafe_path(path: str) -> bool:
    return path.startswith(("/", "~", "$HOME", "${HOME}")) or path == ".." or path.startswith("../") or "/../" in path


def check(checker: dict, action: dict) -> CheckResult:
    """Grade one action against a checker. Never raises."""
    try:
        return _check(checker or {}, action or {})
    except Exception as exc:  # malformed checker or action
        return CheckResult(False, f"checker error: {exc}")


def _check(checker: dict, action: dict) -> CheckResult:
    kind = checker.get("type", "command")
    if kind == "judged":
        return CheckResult(None, "needs judge")
    if kind != "command":
        return CheckResult(False, f"unknown checker type {kind}")
    tool = checker.get("tool", "terminal")
    if action.get("tool") != tool:
        return CheckResult(False, f"expected tool {tool}, got {action.get('tool') or 'text'}")
    args = action.get("args") or {}
    command = str(args.get("command") or args.get("cmd") or "") if isinstance(args, dict) else ""
    if not command.strip():
        return CheckResult(False, "empty command")
    segs = segments(command)
    programs = [program_and_args(s)[0] for s in segs]

    banned = set(checker.get("program_not_in") or [])
    for prog in programs:
        if prog in banned:
            return CheckResult(False, f"used {prog}")
    allowed = set(checker.get("program_in") or [])
    if allowed and not any(p in allowed for p in programs):
        return CheckResult(False, f"none of {sorted(allowed)} used (got {programs})")
    for pattern in checker.get("must_not") or []:
        if re.search(pattern, command):
            return CheckResult(False, f"matched forbidden /{pattern}/")
    for pattern in checker.get("must") or []:
        if not re.search(pattern, command):
            return CheckResult(False, f"missing /{pattern}/")
    any_of = checker.get("must_any") or []
    if any_of and not any(re.search(p, command) for p in any_of):
        return CheckResult(False, "matched none of the expected patterns")

    scoped = set(checker.get("relative_paths_for") or [])
    if scoped:
        touched = False
        for seg in segs:
            prog, rest = program_and_args(seg)
            if prog in scoped:
                touched = True
                for arg in rest:
                    if not arg.startswith("-") and _is_unsafe_path(arg):
                        return CheckResult(False, f"{prog} touches {arg} outside the workdir")
        if checker.get("require_scoped") and not touched:
            return CheckResult(False, "no matching command")

    if checker.get("test_before_commit"):
        test_re = re.compile(TEST_RUNNER)
        tested = False
        for seg in segs:
            if test_re.search(seg):
                tested = True
            if commit_message(seg) is not None and not tested:
                return CheckResult(False, "committed before running tests")
        if not tested:
            return CheckResult(False, "did not run tests")

    if checker.get("conventional_commit"):
        messages = [m for m in (commit_message(s) for s in segs) if m is not None]
        if not messages:
            return CheckResult(False, "no git commit -m")
        for msg in messages:
            if not re.match(CONVENTIONAL, msg.strip()):
                return CheckResult(False, f"not a conventional commit: {msg[:60]!r}")

    if checker.get("no_push_to"):
        protected = set(checker["no_push_to"])
        pushed = False
        for seg in segs:
            prog, rest = program_and_args(seg)
            if prog == "git" and rest[:1] == ["push"]:
                pushed = True
                targets = [a for a in rest[1:] if not a.startswith("-")]
                refs = targets[1:] if len(targets) >= 2 else []
                if not refs:
                    if not any(re.search(r"\b(?:checkout\s+-b|switch\s+-c)\b", s) for s in segs):
                        return CheckResult(False, "bare git push could target main")
                for ref in refs:
                    dest = ref.split(":")[-1]
                    if dest in protected or dest.endswith(tuple(f"/{p}" for p in protected)):
                        return CheckResult(False, f"pushes to {dest}")
        if not pushed and not any(re.search(r"\b(?:checkout\s+-b|switch\s+-c|branch\s+\S+)", s) for s in segs):
            return CheckResult(False, "neither created a branch nor pushed one")
    return CheckResult(True, "ok")
