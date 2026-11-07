"""The brain's compact prompt format, task families, output parsing and action equivalence.

Training (night) and serving (routing) build prompts with the same functions, so the brain sees the
same shape in both. The frontier gets Hermes's full prompt (about 12k tokens in v0.23: 3k system plus
9k of tool schemas). The brain gets a compact one: a short system line, the tools its family needs with
trimmed descriptions, the habits it has not consolidated yet, and the turn so far.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from .probes import program_and_args, segments

BRAIN_SYSTEM = ("You are the Hypnos brain, a small local model that acts for Hermes. Pick the single next "
                "step: call one tool, or answer in one or two short sentences when the work is done.")

# Used when no recorded schema exists (for example synthetic demo data).
DEFAULT_TOOLS: dict[str, dict] = {
    "terminal": {"type": "function", "function": {
        "name": "terminal",
        "description": "Run a shell command in the project's working directory.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
}

_I = re.IGNORECASE
FAMILY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("git", re.compile(r"\b(?:commit|push|pull request|branch|merge|rebase|git|stash|cherry-pick|tag)\b", _I)),
    ("tests", re.compile(r"\b(?:tests?|spec|pytest|vitest|jest|coverage)\b", _I)),
    ("packages", re.compile(r"\b(?:install|uninstall|dependenc(?:y|ies)|packages?|upgrade|lockfile|add \S+ (?:as|to))\b"
                            r"|\b(?:add|remove)\s+(?:the\s+)?[a-z0-9@/._-]+(?:\s+(?:and|as|to)\b|[.!]?$)", _I)),
    ("filesystem", re.compile(r"\b(?:delete|remove|clean|clear|wipe|nuke|get rid of|move|rename|copy)\b.*"
                              r"\b(?:folder|director(?:y|ies)|files?|dir|output|cache|build|dist|logs?)\b", _I)),
    ("build", re.compile(r"\b(?:build|compile|lint|format|dev server|bundle|typecheck|start the)\b", _I)),
]

FAMILY_TOOLS = {"git": ["terminal"], "tests": ["terminal"], "packages": ["terminal"], "filesystem": ["terminal"],
                "build": ["terminal"], "shell": ["terminal"]}


def classify(user_message: str, history: list[dict] | None = None) -> str:
    """Deterministic task family from the user's message. Same function at training and routing time."""
    text = (user_message or "").strip()
    for family, pattern in FAMILY_RULES:
        if pattern.search(text):
            return family
    if re.search(r"`[^`]+`|\b(?:run|execute)\b", text, re.IGNORECASE):
        return "shell"
    return "chat"


def compact_tool(schema: dict) -> dict:
    """Trim a tool schema to what a small model needs: name, first sentence, typed parameters."""
    fn = schema.get("function", schema)
    desc = (fn.get("description") or "").strip().split("\n")[0]
    desc = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0][:160]
    params = fn.get("parameters") or {"type": "object", "properties": {}}
    props = {}
    for name, spec in (params.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        slim = {"type": spec.get("type", "string")}
        if "enum" in spec:
            slim["enum"] = spec["enum"][:12]
        props[name] = slim
    required = [r for r in params.get("required") or [] if r in props]
    return {"type": "function", "function": {"name": fn.get("name", ""), "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


def brain_tools(offered: list[dict] | None, family: str) -> list[dict]:
    """The offered tools this family uses, compacted. Falls back to default schemas."""
    wanted = FAMILY_TOOLS.get(family, ["terminal"])
    by_name = {}
    for schema in offered or []:
        name = (schema.get("function") or schema).get("name")
        if name:
            by_name[name] = schema
    out = []
    for name in wanted:
        schema = by_name.get(name) or DEFAULT_TOOLS.get(name)
        if schema:
            out.append(compact_tool(schema))
    return out


def system_prompt(habits: list[str]) -> str:
    if not habits:
        return BRAIN_SYSTEM
    return BRAIN_SYSTEM + "\n\nHabits to follow:\n" + "\n".join(f"- {h}" for h in habits)


def history_messages(history: list[dict]) -> list[dict]:
    """Convert recorded steps ``{tool, args, result}`` into OpenAI chat messages."""
    messages = []
    for i, step in enumerate(history or []):
        if step.get("text") is not None and not step.get("tool"):
            messages.append({"role": "assistant", "content": step["text"]})
            continue
        call_id = f"call_{i}"
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": step.get("tool", "terminal"), "arguments": step.get("args") or {}}}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": str(step.get("result") or "")[:1200]})
    return messages


def build_messages(user: str, history: list[dict] | None, habits: list[str], previous: list[dict] | None = None) -> list[dict]:
    msgs = [{"role": "system", "content": system_prompt(habits)}]
    for turn in previous or []:
        msgs.append({"role": "user", "content": turn.get("user", "")})
        if turn.get("assistant"):
            msgs.append({"role": "assistant", "content": turn["assistant"]})
    msgs.append({"role": "user", "content": user})
    msgs.extend(history_messages(history or []))
    return msgs


def target_from_action(action: dict) -> dict:
    if action.get("tool"):
        return {"tool_calls": [{"name": action["tool"], "arguments": action.get("args") or {}}]}
    return {"content": action.get("text", "")}


def action_from_target(target: dict) -> dict:
    calls = target.get("tool_calls") or []
    if calls:
        return {"tool": calls[0].get("name"), "args": calls[0].get("arguments") or {}}
    return {"text": target.get("content", "")}


_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_output(text: str) -> dict:
    """Parse raw brain text into an action. Accepts Qwen/Hermes ``<tool_call>`` blocks or a bare JSON call."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    match = _TOOL_CALL.search(text)
    candidate = match.group(1) if match else None
    if candidate is None and text.startswith("{") and '"name"' in text:
        candidate = text[: text.rfind("}") + 1]
    if candidate:
        try:
            data = json.loads(candidate)
            args = data.get("arguments", data.get("parameters", {}))
            if isinstance(args, str):
                args = json.loads(args)
            if isinstance(data.get("name"), str) and isinstance(args, dict):
                return {"tool": data["name"], "args": args}
        except (ValueError, AttributeError):
            return {"invalid": text[:500]}
        return {"invalid": text[:500]}
    if "<tool_call>" in text:
        return {"invalid": text[:500]}
    return {"text": text}


def _norm_command(command: str) -> list[list[str]]:
    out = []
    for seg in segments(command or ""):
        prog, args = program_and_args(seg)
        if prog == "cd":
            continue
        flags = sorted(a for a in args if a.startswith("-"))
        rest = [a.rstrip("/") for a in args if not a.startswith("-")]
        out.append([prog, *rest, *flags])
    return out


def actions_equivalent(a: dict, b: dict) -> bool:
    """Next-action equivalence for the exam: same tool and equivalent arguments.

    Shell commands compare per simple command, ignoring ``cd``, flag order, quoting and trailing slashes.
    Text answers count as equivalent to each other (the exam scores tool choice, not prose).
    """
    if a.get("tool") != b.get("tool"):
        return False
    if not a.get("tool"):
        return bool(a.get("text")) and bool(b.get("text"))
    aa, ba = a.get("args") or {}, b.get("args") or {}
    if "command" in aa or "command" in ba:
        return _norm_command(str(aa.get("command", ""))) == _norm_command(str(ba.get("command", "")))
    return json.dumps(aa, sort_keys=True) == json.dumps(ba, sort_keys=True)


def validate_action(action: dict, tools: list[dict]) -> str:
    """Return '' if the action can be handed to Hermes, else the reason it cannot."""
    if "invalid" in action:
        return "unparseable output"
    if action.get("text") is not None and not action.get("tool"):
        text = action["text"].strip()
        if not text:
            return "empty answer"
        if len(text) > 2000:
            return "answer too long"
        return ""
    names = {(t.get("function") or t).get("name"): (t.get("function") or t) for t in tools or []}
    fn = names.get(action.get("tool"))
    if fn is None:
        return f"tool {action.get('tool')!r} not offered"
    params = fn.get("parameters") or fn.get("input_schema") or {}
    args = action.get("args") or {}
    if not isinstance(args, dict):
        return "arguments are not an object"
    for name in params.get("required") or []:
        if name not in args or args[name] in (None, ""):
            return f"missing required argument {name}"
    props = params.get("properties") or {}
    for name, value in args.items():
        spec = props.get(name)
        if spec is None:
            if params.get("additionalProperties") is False:
                return f"unknown argument {name}"
            continue
        want = spec.get("type")
        ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
              "number": isinstance(value, (int, float)) and not isinstance(value, bool),
              "boolean": isinstance(value, bool), "array": isinstance(value, list),
              "object": isinstance(value, dict)}.get(want, True)
        if not ok:
            return f"argument {name} should be {want}"
        if "enum" in spec and value not in spec["enum"]:
            return f"argument {name} not in enum"
    if action.get("tool") == "terminal":
        try:
            shlex.split(str(args.get("command", "")))
        except ValueError:
            return "command does not parse"
    return ""


def estimate_tokens(value: Any) -> int:
    """Rough token count (4 characters per token) for accounting when no tokenizer is at hand."""
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, len(text) // 4)
