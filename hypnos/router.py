"""Waking routing, as ``llm_execution`` middleware.

The user's normal model (the frontier) stays Hermes's provider. For every LLM call Hypnos either:

- passes it through (``next_call(request)``): no promoted brain, routing off, a family the brain has not
  mastered, an unsupported wire format, or a context too large for the brain;
- runs the brain in *shadow*: the frontier answers, the brain's answer is compared in the background;
- lets the brain answer (*live*): the brain sees a compact prompt with consolidated habits pruned; its
  output must parse, name an offered tool and match that tool's schema, and its confidence must clear the
  threshold. Otherwise Hypnos falls back to the frontier inside the same call.

A brain answer is only ever a proposed tool call or text. Hermes still runs every tool call through its
own approvals; Hypnos never executes tools and never touches approvals.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from . import brainfmt
from . import db as dbmod
from .capture import Event, Writer

log = logging.getLogger("hypnos.router")

HABIT_BLOCK_RE = re.compile(r"\n*Habits learned by Hypnos \(follow them\):\n(?:- .*(?:\n|$))+", re.M)
SUPPORTED_MODES = ("chat_completions", "anthropic_messages")


@dataclass
class BrainState:
    version: int | None = None
    families: dict = field(default_factory=dict)
    habits: list[tuple[int, str]] = field(default_factory=list)
    consolidated: set[int] = field(default_factory=set)
    loaded_at: float = 0.0


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return ""


def strip_habit_block(text: str) -> str:
    return HABIT_BLOCK_RE.sub("", text or "").rstrip()


def _openai_tools(tools: list, api_mode: str) -> list[dict]:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if "function" in tool:
            out.append(tool)
        elif "name" in tool:
            out.append({"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""),
                                                         "parameters": tool.get("input_schema") or {"type": "object"}}})
    return out


def brain_view(request: dict, api_mode: str) -> tuple[str, list[dict], list[dict]] | None:
    """Reduce a frontier request to (user message, steps of this turn, offered tools in OpenAI shape)."""
    messages = request.get("messages") or []
    tools = _openai_tools(request.get("tools") or [], api_mode)
    last_user = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if api_mode == "anthropic_messages" and isinstance(content, list) and content and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue  # tool results travel as user messages on the Anthropic wire
        last_user = i
        break
    if last_user is None:
        return None
    user = strip_habit_block(_text(messages[last_user].get("content")))
    steps: list[dict] = []
    pending: dict[str, dict] = {}
    for msg in messages[last_user + 1:]:
        role, content = msg.get("role"), msg.get("content")
        if api_mode == "anthropic_messages":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    step = {"tool": block.get("name"), "args": block.get("input") or {}, "result": ""}
                    pending[block.get("id", "")] = step
                    steps.append(step)
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    pending[block["tool_use_id"]]["result"] = _text(block.get("content")) or str(block.get("content") or "")
        else:
            if role == "assistant":
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        args = dbmod.loads(args, {"_raw": args})
                    step = {"tool": fn.get("name"), "args": args or {}, "result": ""}
                    pending[call.get("id", "")] = step
                    steps.append(step)
            elif role == "tool" and msg.get("tool_call_id") in pending:
                pending[msg["tool_call_id"]]["result"] = _text(content) or str(content or "")
    for step in steps:
        step["result"] = str(step.get("result") or "")[:600]
    return user, steps, tools


def frontier_action(response: Any, api_mode: str) -> dict:
    """The first tool call (or the text) of a frontier response, for shadow agreement."""
    try:
        if api_mode == "anthropic_messages":
            texts = []
            for block in getattr(response, "content", None) or []:
                if getattr(block, "type", "") == "tool_use":
                    return {"tool": block.name, "args": dict(block.input or {})}
                if getattr(block, "type", "") == "text":
                    texts.append(block.text)
            return {"text": "\n".join(texts)}
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            fn = calls[0].function
            args = fn.arguments
            if isinstance(args, str):
                args = dbmod.loads(args, {})
            return {"tool": fn.name, "args": args or {}}
        return {"text": getattr(message, "content", "") or ""}
    except Exception:
        return {}


def frontier_prompt_tokens(response: Any, api_mode: str) -> int:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    if api_mode == "anthropic_messages":
        return int((getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                   + (getattr(usage, "cache_creation_input_tokens", 0) or 0))
    return int(getattr(usage, "prompt_tokens", 0) or 0)


def provider_response(action: dict, api_mode: str, *, version: int, prompt_tokens: int, completion_tokens: int) -> Any:
    """Shape a brain action like the response Hermes gets from the provider adapter."""
    model = f"hypnos-brain-v{version}"
    if api_mode == "anthropic_messages":
        if action.get("tool"):
            content = [SimpleNamespace(type="tool_use", id=f"toolu_hypnos_{uuid.uuid4().hex[:16]}", name=action["tool"],
                                       input=action.get("args") or {})]
            stop = "tool_use"
        else:
            content = [SimpleNamespace(type="text", text=action.get("text", ""))]
            stop = "end_turn"
        return SimpleNamespace(id=f"msg_hypnos_{uuid.uuid4().hex[:16]}", type="message", role="assistant", model=model,
                               content=content, stop_reason=stop, stop_sequence=None,
                               usage=SimpleNamespace(input_tokens=prompt_tokens, output_tokens=completion_tokens,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=0))
    tool_calls = None
    finish = "stop"
    content = action.get("text")
    if action.get("tool"):
        tool_calls = [SimpleNamespace(id=f"call_hypnos_{uuid.uuid4().hex[:16]}", type="function", extra_content=None,
                                      function=SimpleNamespace(name=action["tool"],
                                                               arguments=json.dumps(action.get("args") or {}),
                                                               args_repaired=False, thought_signature=None))]
        finish = "tool_calls"
        content = None
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls, reasoning_content=None,
                              refusal=None)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                            total_tokens=prompt_tokens + completion_tokens, prompt_tokens_details=None,
                            completion_tokens_details=None)
    return SimpleNamespace(id=f"chatcmpl-hypnos-{uuid.uuid4().hex[:12]}", model=model, usage=usage, provider="hypnos",
                           choices=[SimpleNamespace(index=0, message=message, finish_reason=finish)])


class Router:
    def __init__(self, *, db_path: Callable[[], str], settings: Callable[[], dict], writer: Writer,
                 brain_client: Callable[[str, dict, float], dict], endpoint: Callable[[dict], str],
                 state_ttl: float = 5.0):
        self._db_path = db_path
        self._settings = settings
        self.writer = writer
        self._brain = brain_client
        self._endpoint = endpoint
        self._ttl = state_ttl
        self._states: dict[str, BrainState] = {}
        self._lock = threading.Lock()

    # --- state ---------------------------------------------------------------------------------------

    def state(self, db_path: str) -> BrainState:
        cached = self._states.get(db_path)
        if cached and time.monotonic() - cached.loaded_at < self._ttl:
            return cached
        st = BrainState(loaded_at=time.monotonic())
        try:
            conn = dbmod.connect(db_path, readonly=True)
            try:
                brain = conn.execute("SELECT version, families_json FROM brains WHERE status = 'promoted' "
                                     "ORDER BY version DESC LIMIT 1").fetchone()
                if brain:
                    st.version = brain["version"]
                    st.families = dbmod.loads(brain["families_json"], {})
                    st.habits = [(r["id"], r["statement"]) for r in conn.execute(
                        "SELECT id, statement FROM habits WHERE status = 'active' AND excluded = 0 ORDER BY id")]
                    st.consolidated = {r[0] for r in conn.execute(
                        "SELECT habit_id FROM habit_results WHERE brain_version = ? AND with_text = 0 AND consolidated = 1",
                        (brain["version"],))}
            finally:
                conn.close()
        except Exception:
            pass
        with self._lock:
            self._states[db_path] = st
        return st

    def invalidate(self) -> None:
        with self._lock:
            self._states.clear()

    # --- middleware ----------------------------------------------------------------------------------

    def on_llm_execution(self, *, request: dict, next_call: Callable[[dict], Any], **kw: Any) -> Any:
        started = time.perf_counter()
        try:
            plan = self._plan(request, kw)
        except Exception:
            log.debug("hypnos: routing plan failed", exc_info=True)
            plan = None
        if plan is None or plan["mode"] == "passthrough":
            response = next_call(request)
            self._record_passthrough(request, response, kw, started, plan)
            return response
        if plan["mode"] == "shadow":
            return self._shadow(request, next_call, kw, plan, started)
        return self._live(request, next_call, kw, plan, started)

    def _plan(self, request: dict, kw: dict) -> dict | None:
        settings = self._settings()
        if not settings.get("enabled", True) or settings.get("routing", "auto") == "off":
            return None
        api_mode = kw.get("api_mode") or "chat_completions"
        if api_mode not in SUPPORTED_MODES or kw.get("platform") in (settings.get("exclude_platforms") or []):
            return None
        db_path = self._db_path()
        st = self.state(db_path)
        base = {"mode": "passthrough", "db_path": db_path, "version": st.version}
        if st.version is None:
            return base
        view = brain_view(request, api_mode)
        if view is None:
            return {**base, "reason": "no user message"}
        user, steps, offered = view
        family = brainfmt.classify(user)
        fam = st.families.get(family)
        base["family"] = family
        if not fam:
            return {**base, "reason": f"family {family} not mastered"}
        tools = brainfmt.brain_tools(offered, family)
        offered_names = {(t.get("function") or t).get("name") for t in offered}
        if not tools or any(t["function"]["name"] not in offered_names for t in tools):
            return {**base, "reason": "needed tool not offered"}
        kept = [s for hid, s in st.habits if hid not in st.consolidated]
        pruned = [s for hid, s in st.habits if hid in st.consolidated]
        messages = brainfmt.build_messages(user, steps, kept)
        brain_tokens = brainfmt.estimate_tokens(messages) + brainfmt.estimate_tokens(tools)
        if brain_tokens > int(settings.get("brain_max_context") or 3072):
            return {**base, "reason": "context too large for the brain"}
        state = fam.get("state", "shadow")
        if settings.get("routing", "auto") == "shadow_only" and state == "live":
            state = "shadow"
        return {**base, "mode": "live" if state == "live" else "shadow", "family": family,
                "tools_raw": list(request.get("tools") or []),
                "shadow_agree_turns": int(settings.get("shadow_agree_turns") or 5),
                "routing": settings.get("routing", "auto"),
                "brain_request": {"messages": messages, "tools": tools, "max_tokens": 256, "temperature": 0,
                                  "logprobs": True},
                "offered": offered, "pruned_tokens": brainfmt.estimate_tokens("\n".join(f"- {p}" for p in pruned)) if pruned else 0,
                "brain_tokens_est": brain_tokens, "endpoint": self._endpoint(settings),
                "timeout": float(settings.get("brain_timeout_s") or 20),
                "threshold": float(settings.get("confidence_threshold") or -0.35)}

    def _call_brain(self, plan: dict) -> tuple[dict, dict]:
        """Returns (action, info). Raises on transport errors."""
        t0 = time.perf_counter()
        resp = self._brain(plan["endpoint"], plan["brain_request"], plan["timeout"])
        latency = (time.perf_counter() - t0) * 1000
        message = (resp.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if calls:
            fn = calls[0].get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            action = {"tool": fn.get("name"), "args": args} if isinstance(args, dict) else {"invalid": str(fn)[:300]}
        else:
            action = brainfmt.parse_output(message.get("content") or "")
        hyp = resp.get("hypnos") or {}
        confidence = hyp.get("mean_logprob")
        if confidence is None:
            lp = ((resp.get("choices") or [{}])[0].get("logprobs") or {}).get("content") or []
            values = [x.get("logprob") for x in lp if isinstance(x, dict) and x.get("logprob") is not None]
            confidence = sum(values) / len(values) if values else None
        usage = resp.get("usage") or {}
        return action, {"latency_ms": latency, "confidence": confidence,
                        "prompt_tokens": int(usage.get("prompt_tokens") or plan["brain_tokens_est"]),
                        "completion_tokens": int(usage.get("completion_tokens") or 0)}

    def _live(self, request: dict, next_call: Callable, kw: dict, plan: dict, started: float) -> Any:
        api_mode = kw.get("api_mode") or "chat_completions"
        reason = ""
        try:
            action, info = self._call_brain(plan)
            reason = brainfmt.validate_action(action, plan["offered"])
            if not reason and info["confidence"] is not None and info["confidence"] < plan["threshold"]:
                reason = f"low confidence {info['confidence']:.2f}"
            elif not reason and info["confidence"] is None:
                # No log-probs from this endpoint: require two samples to agree instead.
                second, _ = self._call_brain({**plan, "brain_request": {**plan["brain_request"], "temperature": 0.7}})
                if not brainfmt.actions_equivalent(action, second):
                    reason = "two samples disagreed"
        except Exception as exc:
            action, info = {}, {"latency_ms": None, "confidence": None, "prompt_tokens": 0, "completion_tokens": 0}
            reason = f"brain unavailable: {type(exc).__name__}"
        if reason:
            response = next_call(request)
            self._record(kw, plan, mode="fallback", started=started, frontier_tokens=frontier_prompt_tokens(response, api_mode),
                         info=info, fallback_reason=reason)
            return response
        frontier_est = brainfmt.estimate_tokens(request.get("messages")) + brainfmt.estimate_tokens(request.get("tools"))
        if api_mode == "anthropic_messages":
            frontier_est += brainfmt.estimate_tokens(request.get("system"))
        self._record(kw, plan, mode="brain", started=started, frontier_tokens=frontier_est, info=info, action=action)
        return provider_response(action, api_mode, version=plan["version"], prompt_tokens=info["prompt_tokens"],
                                 completion_tokens=info["completion_tokens"])

    def _shadow(self, request: dict, next_call: Callable, kw: dict, plan: dict, started: float) -> Any:
        api_mode = kw.get("api_mode") or "chat_completions"
        box: dict = {}
        done = threading.Event()

        def run_brain():
            try:
                box["action"], box["info"] = self._call_brain(plan)
            except Exception as exc:
                box["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        ctx = contextvars.copy_context()
        threading.Thread(target=ctx.run, args=(run_brain,), name="hypnos-shadow", daemon=True).start()
        response = next_call(request)
        frontier = frontier_action(response, api_mode)
        tokens = frontier_prompt_tokens(response, api_mode)

        def compare():
            done.wait(plan["timeout"])
            if "action" not in box:
                self._record(kw, plan, mode="shadow", started=started, frontier_tokens=tokens,
                             info=box.get("info") or {}, fallback_reason=box.get("error") or "brain timed out")
                return
            action = box["action"]
            reason = brainfmt.validate_action(action, plan["offered"])
            agreed = (not reason) and brainfmt.actions_equivalent(action, frontier)
            self._record(kw, plan, mode="shadow", started=started, frontier_tokens=tokens, info=box["info"],
                         agreed=agreed, fallback_reason=reason, action=action)

        threading.Thread(target=contextvars.copy_context().run, args=(compare,), name="hypnos-shadow-compare",
                         daemon=True).start()
        return response

    # --- accounting ----------------------------------------------------------------------------------

    def _record_passthrough(self, request: dict, response: Any, kw: dict, started: float, plan: dict | None) -> None:
        try:
            db_path = plan["db_path"] if plan else self._db_path()
            api_mode = kw.get("api_mode") or "chat_completions"
            self.writer.put(Event("route", db_path, {
                "session_id": kw.get("session_id") or "", "turn_id": kw.get("turn_id") or "",
                "api_call": kw.get("api_call_count"), "mode": "passthrough",
                "family": (plan or {}).get("family"), "brain_version": (plan or {}).get("version"),
                "frontier_prompt_tokens": frontier_prompt_tokens(response, api_mode),
                "latency_ms": (time.perf_counter() - started) * 1000,
                "fallback_reason": (plan or {}).get("reason"),
                "tools": list(request.get("tools") or [])}))
        except Exception:
            log.debug("hypnos: route record failed", exc_info=True)

    def _record(self, kw: dict, plan: dict, *, mode: str, started: float, frontier_tokens: int, info: dict,
                agreed: bool | None = None, fallback_reason: str = "", action: dict | None = None) -> None:
        try:
            brain_tokens = int(info.get("prompt_tokens") or 0)
            saved = max(0, frontier_tokens - brain_tokens) if mode == "brain" else 0
            self.writer.put(Event("route", plan["db_path"], {
                "session_id": kw.get("session_id") or "", "turn_id": kw.get("turn_id") or "",
                "api_call": kw.get("api_call_count"), "mode": mode, "family": plan.get("family"),
                "brain_version": plan.get("version"), "frontier_prompt_tokens": frontier_tokens,
                "brain_prompt_tokens": brain_tokens, "tokens_saved": saved, "pruned_tokens": plan.get("pruned_tokens", 0),
                "latency_ms": (time.perf_counter() - started) * 1000, "brain_latency_ms": info.get("latency_ms"),
                "agreed": agreed, "confidence": info.get("confidence"), "fallback_reason": fallback_reason or None,
                "shadow_agree_turns": plan.get("shadow_agree_turns"), "routing": plan.get("routing"),
                "tools": plan.get("tools_raw")}))
        except Exception:
            log.debug("hypnos: route record failed", exc_info=True)
