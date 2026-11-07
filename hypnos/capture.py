"""Daytime capture: the hippocampus.

Hooks run inside the Hermes gateway process, on a fresh thread per call, with the agent waiting.
They do the minimum: copy a few fields and enqueue. A single writer thread scrubs and persists.
Each event carries its own database path, resolved in the hook's context, so one writer can serve
several profiles in a multiplexed gateway.
"""

from __future__ import annotations

import contextvars
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db as dbmod
from .correction import detect
from .outcomes import parse_tool_result
from .scrub import scrub, scrub_text
from .signals import APPROVED_CHOICES, DENIED_CHOICES, GOLD_THRESHOLD, SMART_DENIED, pair_confidence, signal_weight

log = logging.getLogger("hypnos.capture")

MAX_TEXT = 8000
MAX_RESULT = 4000
PAIR_WINDOW_TURNS = 3
DIRECTIVE_PREFIX = "::hypnos"


@dataclass
class Event:
    kind: str
    db_path: str
    data: dict
    ts: float = field(default_factory=time.time)


def _text(value: Any) -> str:
    """User messages may be a list of content parts on multimodal turns."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if value is None else str(value)


def strip_directives(text: str) -> str:
    """Remove the Desktop badge directive Hypnos appends to replies, so it never reaches training data."""
    if not text or DIRECTIVE_PREFIX not in text:
        return text
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith(DIRECTIVE_PREFIX)).rstrip()


def _turn_slice(history: list) -> list[dict]:
    """Messages of the current turn: everything after the last user message."""
    start = 0
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            start = i + 1
            break
    return [dict(m) for m in history[start:] if isinstance(m, dict)]


def _previous_user_message(history: list) -> str:
    seen_current = False
    for msg in reversed(history or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            if seen_current:
                return _text(msg.get("content"))
            seen_current = True
    return ""


class Recorder:
    """Applies capture events to one database. Pure apart from SQLite; tested synchronously."""

    def __init__(self, conn, literals: tuple[str, ...] = ()):
        self.conn = conn
        self.literals = literals
        # Approvals can arrive before the tool result they belong to.
        self._pending_approvals: dict[str, dict] = {}

    # --- helpers -------------------------------------------------------------------------------

    def _scrub(self, value: Any) -> Any:
        return scrub(value, self.literals)

    def _scrub_text(self, text: str, limit: int = MAX_TEXT) -> str:
        text = scrub_text(text or "", self.literals)
        if len(text) > limit:
            half = limit // 2
            text = text[:half] + "\n[...]\n" + text[-half:]
        return text

    def _episode(self, turn_id: str):
        if not turn_id:
            return None
        return self.conn.execute("SELECT * FROM episodes WHERE turn_id = ?", (turn_id,)).fetchone()

    def _signal(self, kind: str, *, ts: float, session_id: str = "", episode_id: int | None = None,
                step_id: int | None = None, confidence: float = 1.0, detail: str = "") -> None:
        polarity, weight = signal_weight(kind, confidence)
        if not weight:
            return
        self.conn.execute(
            "INSERT INTO signals(created_at, session_id, episode_id, step_id, kind, polarity, weight, confidence, detail) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, session_id, episode_id, step_id, kind, polarity, weight, round(confidence, 3), detail[:500]))

    def _touch(self, ts: float) -> None:
        dbmod.set_meta(self.conn, "last_activity", ts)

    # --- events --------------------------------------------------------------------------------

    def apply(self, event: Event) -> None:
        handler = getattr(self, f"on_{event.kind}", None)
        if handler is None:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            handler(event.data, event.ts)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def on_turn_start(self, d: dict, ts: float) -> None:
        session_id = d.get("session_id") or ""
        turn_id = d.get("turn_id") or ""
        if not session_id or not turn_id or self._episode(turn_id):
            return
        self.conn.execute(
            "INSERT INTO sessions(session_id, platform, parent_session_id, turn_count, first_seen, last_seen) "
            "VALUES(?,?,?,0,?,?) ON CONFLICT(session_id) DO UPDATE SET last_seen = excluded.last_seen",
            (session_id, d.get("platform") or "", d.get("parent_session_id") or "", ts, ts))
        self.conn.execute("UPDATE sessions SET turn_count = turn_count + 1 WHERE session_id = ?", (session_id,))
        turn_index = self.conn.execute(
            "SELECT turn_count FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]
        message = self._scrub_text(_text(d.get("user_message")))
        previous = self.conn.execute(
            "SELECT id, user_message FROM episodes WHERE session_id = ? ORDER BY turn_index DESC LIMIT 1",
            (session_id,)).fetchone()
        prev_text = d.get("previous_user_message") or (previous["user_message"] if previous else "")
        verdict = detect(message, self._scrub_text(prev_text)) if previous or prev_text else None
        correction_of = previous["id"] if (verdict and verdict.negative and previous) else None
        self.conn.execute(
            "INSERT INTO episodes(turn_id, session_id, turn_index, platform, model, started_at, user_message, "
            "correction_of, source) VALUES(?,?,?,?,?,?,?,?,?)",
            (turn_id, session_id, turn_index, d.get("platform") or "", d.get("model") or "", ts, message,
             correction_of, d.get("source") or "live"))
        if verdict and verdict.negative and previous:
            kind = "correction" if verdict.is_correction else "reask"
            self._signal(kind, ts=ts, session_id=session_id, episode_id=previous["id"],
                         confidence=verdict.confidence, detail=", ".join(verdict.matched)[:200])
        self._touch(ts)

    def _ensure_step(self, episode_id: int, tool_call_id: str, tool_name: str, args: Any, ts: float):
        row = None
        if tool_call_id:
            row = self.conn.execute(
                "SELECT * FROM steps WHERE episode_id = ? AND tool_call_id = ?", (episode_id, tool_call_id)).fetchone()
        if row:
            return row
        index = self.conn.execute(
            "SELECT COALESCE(MAX(step_index), -1) + 1 FROM steps WHERE episode_id = ?", (episode_id,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO steps(episode_id, step_index, kind, tool_call_id, tool_name, args_json, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (episode_id, index, "tool", tool_call_id or None, tool_name, dbmod.dumps(self._scrub(args or {})), ts))
        return self.conn.execute("SELECT * FROM steps WHERE episode_id = ? AND step_index = ?",
                                 (episode_id, index)).fetchone()

    def on_tool(self, d: dict, ts: float) -> None:
        episode = self._episode(d.get("turn_id") or "")
        if episode is None:
            # Background-review forks and similar fire tool hooks for turns that were never opened.
            return
        tool_call_id = d.get("tool_call_id") or ""
        tool_name = d.get("tool_name") or ""
        args = d.get("args") if isinstance(d.get("args"), dict) else {}
        step = self._ensure_step(episode["id"], tool_call_id, tool_name, args, ts)
        out = parse_tool_result(tool_name, d.get("result"), args, d.get("status") or "", d.get("error_message") or "")
        result_text = out.error if out.denied else (out.output or out.error)
        approval = step["approval"]
        if out.denied and not approval:
            approval = "denied"
        elif out.approved_note and not approval:
            approval = "approved"
        tests = out.tests
        self.conn.execute(
            "UPDATE steps SET result_text = ?, exit_code = ?, outcome = ?, approval = ?, test_framework = ?, "
            "tests_passed = ?, tests_failed = ?, duration_ms = ? WHERE id = ?",
            (self._scrub_text(result_text, MAX_RESULT), out.exit_code, out.polarity, approval,
             tests.framework if tests else None, tests.passed if tests else None, tests.failed if tests else None,
             d.get("duration_ms"), step["id"]))
        session_id = episode["session_id"]
        pending = self._pending_approvals.pop(tool_call_id, None) if tool_call_id else None
        if pending:
            self._apply_approval(step["id"], episode, pending, ts)
        elif out.denied and step["approval"] is None:
            self._signal("approval_denied", ts=ts, session_id=session_id, episode_id=episode["id"],
                         step_id=step["id"], detail=out.deny_reason)
        if tests:
            self._signal("test_pass" if tests.ok else "test_fail", ts=ts, session_id=session_id,
                         episode_id=episode["id"], step_id=step["id"], detail=f"{tests.framework} {tests.passed}/{tests.failed}")
        elif not out.denied and out.polarity:
            self._signal("tool_pass" if out.polarity > 0 else "tool_fail", ts=ts, session_id=session_id,
                         episode_id=episode["id"], step_id=step["id"])
        self._touch(ts)

    def on_approval(self, d: dict, ts: float) -> None:
        tool_call_id = d.get("tool_call_id") or ""
        if not tool_call_id:
            return
        step = self.conn.execute("SELECT * FROM steps WHERE tool_call_id = ? ORDER BY id DESC LIMIT 1",
                                 (tool_call_id,)).fetchone()
        if step is None:
            self._pending_approvals[tool_call_id] = d
            if len(self._pending_approvals) > 256:
                self._pending_approvals.pop(next(iter(self._pending_approvals)))
            return
        episode = self.conn.execute("SELECT * FROM episodes WHERE id = ?", (step["episode_id"],)).fetchone()
        self._apply_approval(step["id"], episode, d, ts)

    def _apply_approval(self, step_id: int, episode, d: dict, ts: float) -> None:
        choice = (d.get("choice") or "").lower()
        if choice in APPROVED_CHOICES:
            approval, kind = "approved", "approval_granted"
        elif choice in DENIED_CHOICES:
            approval, kind = "denied", "approval_denied"
        elif choice in SMART_DENIED:
            approval, kind = "smart_denied", "smart_denied"
        elif choice == "smart_approve":
            approval, kind = "auto", ""
        else:
            return
        self.conn.execute("UPDATE steps SET approval = ? WHERE id = ?", (approval, step_id))
        self.conn.execute("DELETE FROM signals WHERE step_id = ? AND kind = 'approval_denied'", (step_id,))
        if kind:
            self._signal(kind, ts=ts, session_id=episode["session_id"], episode_id=episode["id"], step_id=step_id)

    def on_turn_end(self, d: dict, ts: float) -> None:
        episode = self._episode(d.get("turn_id") or "")
        if episode is None:
            return
        # Fill in steps from the transcript: exact args as the model produced them, plus final text.
        for msg in d.get("turn_messages") or []:
            if msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                args = fn.get("arguments")
                if isinstance(args, str):
                    args = dbmod.loads(args, {"_raw": args})
                self._ensure_step(episode["id"], call.get("id") or "", fn.get("name") or "", args, ts)
        text = strip_directives(_text(d.get("assistant_response")))
        if text:
            index = self.conn.execute(
                "SELECT COALESCE(MAX(step_index), -1) + 1 FROM steps WHERE episode_id = ?", (episode["id"],)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO steps(episode_id, step_index, kind, text, outcome, created_at) VALUES(?,?,?,?,0,?)",
                (episode["id"], index, "text", self._scrub_text(text), ts))
        self.conn.execute(
            "UPDATE episodes SET status = 'ok', ended_at = ?, assistant_text = ? WHERE id = ?",
            (ts, self._scrub_text(text), episode["id"]))
        self._match_pairs(episode, ts)
        self._touch(ts)

    def on_turn_close(self, d: dict, ts: float) -> None:
        episode = self._episode(d.get("turn_id") or "")
        if episode is None or episode["status"] != "open":
            return
        status = "interrupted" if d.get("interrupted") else ("failed" if d.get("failed") else "ok")
        self.conn.execute("UPDATE episodes SET status = ?, ended_at = ? WHERE id = ?", (status, ts, episode["id"]))
        if status == "ok":
            self._match_pairs(episode, ts)

    def on_link(self, d: dict, ts: float) -> None:
        old, new = d.get("old_session_id") or "", d.get("session_id") or ""
        if old and new and old != new:
            self.conn.execute(
                "INSERT INTO sessions(session_id, linked_from, turn_count, first_seen, last_seen) "
                "SELECT ?, ?, turn_count, ?, ? FROM sessions WHERE session_id = ? "
                "ON CONFLICT(session_id) DO UPDATE SET linked_from = excluded.linked_from",
                (new, old, ts, ts, old))

    def on_purge(self, d: dict, ts: float) -> None:
        for session_id in d.get("session_ids") or []:
            self.conn.execute("DELETE FROM episodes WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM signals WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM pairs WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def on_mark(self, d: dict, ts: float) -> None:
        session_id = d.get("session_id") or ""
        query = "SELECT * FROM episodes WHERE status != 'open' {} ORDER BY started_at DESC LIMIT 1"
        if session_id:
            episode = self.conn.execute(query.format("AND session_id = ?"), (session_id,)).fetchone()
        else:
            episode = self.conn.execute(query.format("")).fetchone()
        if episode is None:
            return
        kind = "mark_good" if d.get("good") else "mark_bad"
        self._signal(kind, ts=ts, session_id=episode["session_id"], episode_id=episode["id"],
                     detail=self._scrub_text(d.get("note") or "", 300))

    # --- pairs ---------------------------------------------------------------------------------

    @staticmethod
    def _action(step) -> dict:
        if step["kind"] == "text":
            return {"text": step["text"] or ""}
        return {"tool": step["tool_name"], "args": dbmod.loads(step["args_json"], {})}

    def _insert_pair(self, *, source: str, episode, context_episode: int, context_step: int, rejected, chosen,
                     confidence: float, ts: float, detail: str = "") -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM pairs WHERE rejected_step_id = ? AND chosen_step_id = ?", (rejected["id"], chosen["id"])).fetchone()
        if exists:
            return
        gold = 1 if confidence >= GOLD_THRESHOLD else 0
        self.conn.execute(
            "INSERT INTO pairs(created_at, source, session_id, context_episode, context_step, chosen_json, rejected_json, "
            "chosen_step_id, rejected_step_id, confidence, gold, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, source, episode["session_id"], context_episode, context_step, dbmod.dumps(self._action(chosen)),
             dbmod.dumps(self._action(rejected)), chosen["id"], rejected["id"], confidence, gold, detail))
        self._signal("gold_pair" if gold else "pair", ts=ts, session_id=episode["session_id"],
                     episode_id=context_episode, step_id=rejected["id"], confidence=confidence, detail=source)

    def _match_pairs(self, episode, ts: float) -> None:
        """Pair denied or corrected actions with the accepted action that replaced them."""
        session_id = episode["session_id"]
        turn_index = episode["turn_index"]
        candidates = self.conn.execute(
            "SELECT s.*, e.turn_index FROM steps s JOIN episodes e ON e.id = s.episode_id "
            "WHERE e.session_id = ? AND e.turn_index BETWEEN ? AND ? AND s.kind = 'tool' "
            "ORDER BY e.turn_index, s.step_index",
            (session_id, turn_index - PAIR_WINDOW_TURNS, turn_index)).fetchall()
        accepted = [s for s in candidates if s["approval"] in ("approved", "auto", None) and s["outcome"] >= 0
                    and s["result_text"] is not None]
        corrections = {
            row["id"]: row for row in self.conn.execute(
                "SELECT e.id, e.correction_of, MAX(sg.confidence) AS conf FROM episodes e "
                "LEFT JOIN signals sg ON sg.episode_id = e.correction_of AND sg.kind IN ('correction', 'reask') "
                "WHERE e.session_id = ? AND e.turn_index BETWEEN ? AND ? GROUP BY e.id",
                (session_id, turn_index - PAIR_WINDOW_TURNS, turn_index)).fetchall()}
        # 1. Denied action, later replaced by an accepted one.
        for denied in (s for s in candidates if s["approval"] == "denied"):
            best, best_conf = None, 0.0
            for chosen in accepted:
                if (chosen["turn_index"], chosen["step_index"]) <= (denied["turn_index"], denied["step_index"]):
                    continue
                gap = chosen["turn_index"] - denied["turn_index"]
                corr = corrections.get(chosen["episode_id"])
                corr_conf = float(corr["conf"] or 0) if corr and corr["correction_of"] == denied["episode_id"] else 0.0
                if gap == 0:
                    corr_conf = max(corr_conf, 0.3)
                conf = pair_confidence(self._action(denied), self._action(chosen), source="denial",
                                       correction_conf=corr_conf, explicit_approval=chosen["approval"] == "approved",
                                       chosen_outcome=chosen["outcome"], turn_gap=max(1, gap))
                if conf > best_conf:
                    best, best_conf = chosen, conf
            if best is not None and best_conf >= 0.5:
                self._insert_pair(source="denial", episode=episode, context_episode=denied["episode_id"],
                                  context_step=denied["step_index"], rejected=denied, chosen=best,
                                  confidence=best_conf, ts=ts)
        # 2. User corrected the previous turn; this turn's matching action is the fix.
        corr = corrections.get(episode["id"])
        if corr and corr["correction_of"]:
            prev_steps = [s for s in candidates if s["episode_id"] == corr["correction_of"] and s["approval"] != "denied"]
            mine = [s for s in accepted if s["episode_id"] == episode["id"]]
            for rejected in reversed(prev_steps):
                best, best_conf = None, 0.0
                for chosen in mine:
                    conf = pair_confidence(self._action(rejected), self._action(chosen), source="correction",
                                           correction_conf=float(corr["conf"] or 0),
                                           explicit_approval=chosen["approval"] == "approved",
                                           chosen_outcome=chosen["outcome"])
                    if conf > best_conf:
                        best, best_conf = chosen, conf
                if best is not None and best_conf >= 0.5:
                    self._insert_pair(source="correction", episode=episode, context_episode=rejected["episode_id"],
                                      context_step=rejected["step_index"], rejected=rejected, chosen=best,
                                      confidence=best_conf, ts=ts)
                    break


class Writer:
    """Single background thread that drains the queue into the right database."""

    def __init__(self, literals: Callable[[], tuple[str, ...]] = lambda: ()):
        self.queue: queue.Queue[Event | None] = queue.Queue(maxsize=10000)
        self._literals = literals
        self._recorders: dict[str, Recorder] = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.dropped = 0
        self.errors = 0

    def put(self, event: Event) -> None:
        self._ensure_thread()
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            ctx = contextvars.copy_context()
            self._thread = threading.Thread(target=ctx.run, args=(self._run,), name="hypnos-capture", daemon=True)
            self._thread.start()

    def recorder(self, db_path: str) -> Recorder:
        rec = self._recorders.get(db_path)
        if rec is None:
            rec = Recorder(dbmod.open_db(db_path), self._literals())
            self._recorders[db_path] = rec
        return rec

    def process(self, event: Event) -> None:
        try:
            self.recorder(event.db_path).apply(event)
        except Exception:
            self.errors += 1
            log.warning("hypnos: failed to record %s event", event.kind, exc_info=True)

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            try:
                if event is None:
                    return
                self.process(event)
            finally:
                self.queue.task_done()

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until queued events are written. Returns False on timeout."""
        deadline = time.time() + timeout
        while self.queue.unfinished_tasks:
            if time.time() > deadline:
                return False
            time.sleep(0.01)
        return True


def _trace(name: str, kwargs: dict) -> None:
    """Opt-in debugging: ``HYPNOS_TRACE=/path/file`` appends one line per hook call (field names only)."""
    path = os.environ.get("HYPNOS_TRACE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f} {os.getpid()} {name} {sorted(kwargs)}\n")
    except OSError:
        pass


class Capture:
    """Hook callbacks. Each one is cheap, fail-open and accepts ``**kwargs``."""

    def __init__(self, *, db_path: Callable[[], str], settings: Callable[[], dict], writer: Writer):
        self._db_path = db_path
        self._settings = settings
        self.writer = writer

    def _active(self, platform: str = "", parent_session_id: str = "") -> dict | None:
        settings = self._settings()
        if not settings.get("enabled", True):
            return None
        if platform and platform in (settings.get("exclude_platforms") or []):
            return None
        if parent_session_id and not settings.get("capture_subagents", False):
            return None
        return settings

    def _emit(self, kind: str, data: dict) -> None:
        self.writer.put(Event(kind, self._db_path(), data))

    def hooks(self) -> dict[str, Callable[..., Any]]:
        return {
            "pre_llm_call": self.on_pre_llm_call,
            "post_tool_call": self.on_post_tool_call,
            "post_approval_response": self.on_post_approval_response,
            "post_llm_call": self.on_post_llm_call,
            "on_session_end": self.on_session_end,
            "on_session_finalize": self.on_session_finalize,
            "post_compaction": self.on_post_compaction,
            "on_session_delete": self.on_session_delete,
        }

    def on_pre_llm_call(self, **kwargs: Any) -> None:
        _trace("on_pre_llm_call", kwargs)
        try:
            if self._active(kwargs.get("platform") or "", kwargs.get("parent_session_id") or "") is None:
                return None
            history = kwargs.get("conversation_history") or []
            self._emit("turn_start", {
                "session_id": kwargs.get("session_id") or "",
                "turn_id": kwargs.get("turn_id") or "",
                "user_message": kwargs.get("user_message"),
                "previous_user_message": _previous_user_message(history),
                "platform": kwargs.get("platform") or "",
                "model": kwargs.get("model") or "",
                "parent_session_id": kwargs.get("parent_session_id") or "",
            })
        except Exception:
            log.debug("hypnos: pre_llm_call capture failed", exc_info=True)
        return None

    def on_post_tool_call(self, **kwargs: Any) -> None:
        _trace("on_post_tool_call", kwargs)
        try:
            if self._active() is None:
                return None
            args = kwargs.get("args")
            self._emit("tool", {
                "turn_id": kwargs.get("turn_id") or "",
                "tool_call_id": kwargs.get("tool_call_id") or "",
                "tool_name": kwargs.get("tool_name") or "",
                "args": dict(args) if isinstance(args, dict) else {},
                "result": kwargs.get("result"),
                "status": kwargs.get("status") or "",
                "error_message": kwargs.get("error_message") or "",
                "duration_ms": kwargs.get("duration_ms"),
            })
        except Exception:
            log.debug("hypnos: post_tool_call capture failed", exc_info=True)
        return None

    def on_post_approval_response(self, **kwargs: Any) -> None:
        _trace("on_post_approval_response", kwargs)
        try:
            if self._active() is None:
                return None
            self._emit("approval", {
                "tool_call_id": kwargs.get("tool_call_id") or "",
                "turn_id": kwargs.get("turn_id") or "",
                "choice": kwargs.get("choice") or "",
                "surface": kwargs.get("surface") or "",
            })
        except Exception:
            log.debug("hypnos: approval capture failed", exc_info=True)
        return None

    def on_post_llm_call(self, **kwargs: Any) -> None:
        _trace("on_post_llm_call", kwargs)
        try:
            if self._active(kwargs.get("platform") or "") is None:
                return None
            self._emit("turn_end", {
                "turn_id": kwargs.get("turn_id") or "",
                "assistant_response": kwargs.get("assistant_response"),
                "turn_messages": _turn_slice(kwargs.get("conversation_history") or []),
            })
        except Exception:
            log.debug("hypnos: post_llm_call capture failed", exc_info=True)
        return None

    def on_session_end(self, **kwargs: Any) -> None:
        _trace("on_session_end", kwargs)
        try:
            if self._active() is None or not kwargs.get("turn_id"):
                return None
            self._emit("turn_close", {
                "turn_id": kwargs.get("turn_id") or "",
                "completed": bool(kwargs.get("completed")),
                "failed": bool(kwargs.get("failed")),
                "interrupted": bool(kwargs.get("interrupted")),
            })
        except Exception:
            log.debug("hypnos: on_session_end capture failed", exc_info=True)
        return None

    def on_session_finalize(self, **kwargs: Any) -> None:
        _trace("on_session_finalize", kwargs)
        try:
            self.writer.flush(timeout=2.0)
        except Exception:
            log.debug("hypnos: flush failed", exc_info=True)
        return None

    def on_post_compaction(self, **kwargs: Any) -> None:
        _trace("on_post_compaction", kwargs)
        try:
            if kwargs.get("old_session_id") and kwargs.get("session_id"):
                self._emit("link", {"old_session_id": kwargs["old_session_id"], "session_id": kwargs["session_id"]})
        except Exception:
            log.debug("hypnos: post_compaction capture failed", exc_info=True)
        return None

    def on_session_delete(self, **kwargs: Any) -> None:
        _trace("on_session_delete", kwargs)
        try:
            ids = list(kwargs.get("deleted_ids") or [])
            if kwargs.get("session_id"):
                ids.append(kwargs["session_id"])
            if ids:
                self._emit("purge", {"session_ids": ids})
        except Exception:
            log.debug("hypnos: on_session_delete capture failed", exc_info=True)
        return None

    def mark(self, good: bool, session_id: str = "", note: str = "") -> None:
        self._emit("mark", {"good": good, "session_id": session_id, "note": note})
