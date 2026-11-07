"""Render brain examples with the base model's own chat template.

Models whose template understands ``tools`` (Qwen3, Llama 3.x, Hermes 3, ...) are rendered natively, so
the brain learns the tool-calling format it was pretrained on. Models without tool support get a
Hermes-style fallback: tool schemas in the system message, calls as ``<tool_call>{json}</tool_call>``.
"""

from __future__ import annotations

import json
from typing import Any


def supports_tools(tokenizer: Any) -> bool:
    template = getattr(tokenizer, "chat_template", None) or ""
    if isinstance(template, dict):
        template = " ".join(str(v) for v in template.values())
    return "tools" in template and "tool_call" in template


def _fallback_messages(messages: list[dict], tools: list[dict]) -> list[dict]:
    out = []
    tool_text = "\n".join(json.dumps(t.get("function", t), ensure_ascii=False) for t in tools or [])
    instructions = ("You can call these tools:\n<tools>\n" + tool_text + "\n</tools>\n"
                    'To call a tool, reply with only <tool_call>{"name": "...", "arguments": {...}}</tool_call>.')
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "system":
            out.append({"role": "system", "content": f"{msg.get('content', '')}\n\n{instructions}" if tools else msg.get("content", "")})
        elif role == "assistant" and msg.get("tool_calls"):
            calls = []
            for call in msg["tool_calls"]:
                fn = call.get("function", call)
                calls.append("<tool_call>" + json.dumps({"name": fn.get("name"), "arguments": fn.get("arguments", {})},
                                                        ensure_ascii=False) + "</tool_call>")
            out.append({"role": "assistant", "content": "\n".join(calls)})
        elif role == "tool":
            out.append({"role": "user", "content": f"<tool_response>\n{msg.get('content', '')}\n</tool_response>"})
        else:
            out.append({"role": role, "content": msg.get("content", "") or ""})
        if i == 0 and role != "system" and tools:
            out.insert(0, {"role": "system", "content": instructions})
    return out


def _assistant_message(target: dict) -> dict:
    calls = target.get("tool_calls") or []
    if calls:
        return {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": c["name"], "arguments": c.get("arguments", {})}} for c in calls]}
    return {"role": "assistant", "content": target.get("content", "")}


def _template(tokenizer: Any, messages: list[dict], tools: list[dict] | None, generation: bool) -> str:
    kwargs = {"tokenize": False, "add_generation_prompt": generation, "enable_thinking": False}
    if supports_tools(tokenizer):
        return tokenizer.apply_chat_template(messages, tools=tools or None, **kwargs)
    return tokenizer.apply_chat_template(_fallback_messages(messages, tools or []), **kwargs)


def render_prompt(tokenizer: Any, messages: list[dict], tools: list[dict] | None) -> str:
    return _template(tokenizer, messages, tools, True)


def render_completion(tokenizer: Any, messages: list[dict], tools: list[dict] | None, target: dict) -> str:
    """The exact text the model should produce after ``render_prompt``, including the end-of-turn token."""
    prompt = render_prompt(tokenizer, messages, tools)
    full = _template(tokenizer, messages + [_assistant_message(target)], tools, False)
    if full.startswith(prompt):
        completion = full[len(prompt):]
    else:
        calls = target.get("tool_calls") or []
        if calls:
            completion = "\n".join("<tool_call>\n" + json.dumps({"name": c["name"], "arguments": c.get("arguments", {})},
                                                                ensure_ascii=False) + "\n</tool_call>" for c in calls)
        else:
            completion = target.get("content", "")
        completion += tokenizer.eos_token or ""
    # Drop trailing whitespace after the end-of-turn token: it is not something the model should learn to emit.
    return completion.rstrip("\n")
