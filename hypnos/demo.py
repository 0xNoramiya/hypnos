"""``hermes hypnos demo timelapse``: several simulated days and real nights, labeled SYNTHETIC everywhere.

A scripted developer persona works in a throwaway project. Its frontier model starts out with generic
habits (npm, committing before tests, free-form commit messages, pushing to main, deleting outside the
project); the persona corrects or denies each one the first time. Every turn goes through the real capture
pipeline and every LLM call through the real routing middleware, with a request the size of a Hermes v0.23
one (about 12k prompt tokens). Between days Hypnos really sleeps: dreams in the sandbox, training in the
worker, the exam, promotion, the brain server. By the last day the brain answers the turns it has mastered.

Nothing here is real user data: episodes are stored with ``source = 'synthetic'`` and every report says so.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import brainfmt
from . import db as dbmod
from .capture import Event, HabitInjector, Writer
from .habits import active_habits
from .probes import check
from .router import Router, provider_response

TOOLS = json.loads((Path(__file__).parent / "demo_tools.json").read_text())

_GUIDANCE = [
    "Prefer the dedicated file tools over shell equivalents when reading, searching or editing files.",
    "Keep the user informed with short progress notes during long tasks, and never invent results.",
    "Ask before destructive actions; respect the approval system and never route around it.",
    "When a command fails, read the error, fix the cause, and only then retry.",
    "Summarise what you changed at the end of a task in one or two sentences.",
    "Use the project's own scripts and conventions; read package.json, pyproject.toml or Makefile first.",
    "Never print secrets; redact tokens and keys in anything you show.",
    "Work in the current project directory unless the user names another path.",
]
SYSTEM = ("SYNTHETIC stand-in for a Hermes system prompt, sized like the real one.\n" + "\n".join(
    f"{i + 1}. {_GUIDANCE[i % len(_GUIDANCE)]} (rule {i + 1})" for i in range(118)))

PACKAGES = ["zod", "axios", "dayjs", "nanoid", "chalk", "express", "yaml", "commander", "ky", "uuid", "ora", "got",
            "lru-cache", "p-limit", "picocolors", "date-fns"]
AREAS = ["API", "web app", "CLI", "worker", "admin panel"]
TOPICS = ["login form", "search box", "rate limiter", "csv export", "dark mode", "retry policy", "billing page",
          "webhook handler", "settings page", "error toast"]
CORRECTIONS = {
    "pkg_manager:pnpm": ["no, we use pnpm here", "bukan, pakai pnpm aja", "use pnpm instead, not npm"],
    "git:tests_before_commit": ["no, run the tests first", "jangan commit dulu, jalankan test dulu"],
    "git:conventional_commits": ["use a conventional commit message, like feat(auth): ...",
                                 "salah, pakai conventional commit"],
    "git:no_push_main": ["never push to main, make a feature branch", "jangan push ke main, bikin branch baru"],
    "safe_delete:workdir": ["only delete inside this project", "bukan itu, hapus yang di folder project aja"],
}
APPROVAL_PATTERNS = ("rm -rf", "git push")


@dataclass
class Task:
    user: str
    habit: str | None
    default: str
    correct: str
    output: str
    family_hint: str = ""


def slug(text: str) -> str:
    return "-".join(text.lower().split())


def day_tasks(day: int, rng: random.Random) -> list[list[Task]]:
    """Task chains for one day. A chain shares a session, so a correction lands on the turn it fixes."""
    chains: list[list[Task]] = []

    def pkg():
        p, a = rng.choice(PACKAGES), rng.choice(AREAS)
        return [Task(f"Add {p} to the {a}.", "pkg_manager:pnpm", f"npm install {p}", f"pnpm add {p}", f"+ {p} 1.4.0")]

    def commit():
        t = rng.choice(TOPICS)
        scope = t.split()[0]
        return [Task(f"Commit the {t} changes.", "git:tests_before_commit", f'git commit -am "{t} changes"', "pnpm test",
                     "Tests  12 passed (12)"),
                Task("Tests are green, commit it now.", "git:conventional_commits", f'git commit -am "{t} changes"',
                     f'git commit -am "feat({scope}): {t}"', f"[feat/{slug(t)} 3f2c1a9] feat({scope}): {t}")]

    def push():
        t = rng.choice(TOPICS)
        return [Task(f"Push the {t} work.", "git:no_push_main", "git push origin main",
                     f"git checkout -b feat/{slug(t)} && git push -u origin feat/{slug(t)}",
                     f"* [new branch] feat/{slug(t)} -> feat/{slug(t)}")]

    def delete():
        d = rng.choice(["build", "dist", "coverage", ".cache"])
        return [Task(f"Clean up the old {d} output.", "safe_delete:workdir", f"rm -rf ../app-backup/{d}", f"rm -rf ./{d}", "")]

    def routine():
        user, cmd, out = rng.choice([("Run the tests.", "pnpm test", "Tests  12 passed (12)"),
                                     ("Show me the git status.", "git status -sb", "## feat/x\n M src/app.ts"),
                                     ("Lint the project.", "pnpm lint", "0 problems"),
                                     ("Start the dev server.", "pnpm dev", "ready on http://localhost:3000"),
                                     ("What changed in the last commit?", "git show --stat HEAD", "1 file changed")])
        return [Task(user, None, cmd, cmd, out)]

    plan = [pkg, routine, commit, push, delete, pkg, routine, pkg, commit, routine, pkg, push, delete, routine]
    if day > 1:
        rng.shuffle(plan)
    for make in plan:
        chains.append(make())
    return chains


class Persona:
    """Decides what the scripted user does: approve, deny, correct. Knows the habits it cares about."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def follows(self, task: Task, action: dict) -> bool:
        if not action.get("tool"):
            return False
        if task.habit is None:
            return brainfmt.actions_equivalent(action, {"tool": "terminal", "args": {"command": task.correct}})
        from .habits import (
            branch_habit,
            conventional_commit_habit,
            pkg_manager_habit,
            safe_delete_habit,
            tests_first_habit,
        )
        spec = {"pkg_manager:pnpm": lambda: pkg_manager_habit("pnpm", "demo"),
                "git:tests_before_commit": lambda: tests_first_habit("demo"),
                "git:conventional_commits": lambda: conventional_commit_habit("demo"),
                "git:no_push_main": lambda: branch_habit("demo"),
                "safe_delete:workdir": lambda: safe_delete_habit("demo")}[task.habit]()
        return bool(check(spec.probes[0].checker, action).passed)

    def correction(self, habit: str) -> str:
        return self.rng.choice(CORRECTIONS[habit])


class DemoFrontier:
    """The persona's normal model: generic habits, but it reads Hermes's habit block and corrections."""

    def __init__(self, known: Callable[[], set[str]]):
        self.known = known
        self.task: Task | None = None
        self.corrected = False

    def __call__(self, request: dict):
        messages = request.get("messages") or []
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            content = str(last.get("content") or "")
            text = "Okay, I won't run that. Tell me how you'd like to proceed." if "BLOCKED" in content else "Done."
            action = {"text": text}
        else:
            task = self.task
            user = str(last.get("content") or "")
            habit_known = task.habit is None or task.habit in self.known() or self.corrected
            statement_seen = "Habits learned by Hypnos" in user
            follow = habit_known and (statement_seen or self.corrected or task.habit is None)
            action = {"tool": "terminal", "args": {"command": task.correct if follow else task.default}}
        tokens = brainfmt.estimate_tokens(messages) + brainfmt.estimate_tokens(request.get("tools"))
        response = provider_response(action, "chat_completions", version=0, prompt_tokens=tokens, completion_tokens=24)
        response.model, response.provider = "demo-frontier", "demo"
        return response


@dataclass
class DayStats:
    turns: int = 0
    corrections: int = 0
    denials: int = 0
    brain_calls: int = 0
    calls: int = 0


class Timelapse:
    def __init__(self, *, db_path: str, data_dir: Path, settings: dict, days: int = 5, tiny: bool = False,
                 out: Callable[[str], None] = print, seed: int = 7, brain_client=None, night_factory=None,
                 start_server: Callable[[dict], None] | None = None):
        self.db_path = db_path
        self.data_dir = data_dir
        self.days = days
        self.tiny = tiny
        self.out = out
        self.rng = random.Random(seed)
        self.persona = Persona(self.rng)
        self.writer = Writer()
        self.injector = HabitInjector(ttl=0)
        self.writer.on_habits_changed = self.injector.invalidate
        self.settings = {"enabled": True, "routing": "auto", "shadow_agree_turns": 3, "confidence_threshold": -0.5,
                         "brain_timeout_s": 90, "brain_max_context": 3072, "exclude_platforms": [], **settings}
        from . import brainserver
        self.router = Router(db_path=lambda: db_path, settings=lambda: self.settings, writer=self.writer,
                             brain_client=brain_client or (lambda ep, body, t: brainserver.http_json(
                                 ep.rstrip("/") + "/chat/completions", body, timeout=t)),
                             endpoint=lambda s: brainserver.endpoint(data_dir, str(s.get("brain_endpoint") or "")),
                             state_ttl=0)
        self.writer.on_brain_changed = self.router.invalidate
        self.frontier = DemoFrontier(self._known_habits)
        self.night_factory = night_factory
        self.start_server = start_server

    def _known_habits(self) -> set[str]:
        conn = dbmod.connect(self.db_path, readonly=True)
        try:
            return {h["key"] for h in active_habits(conn)}
        finally:
            conn.close()

    def _put(self, kind: str, data: dict, ts: float) -> None:
        self.writer.put(Event(kind, self.db_path, data, ts))

    def _settle(self) -> None:
        for t in [t for t in threading.enumerate() if t.name.startswith("hypnos-shadow")]:
            t.join(timeout=self.settings["brain_timeout_s"] + 5)
        self.writer.flush(30)

    def _call(self, messages: list, session: str, turn_id: str, n: int) -> dict:
        request = {"model": "frontier", "messages": messages, "tools": TOOLS}
        response = self.router.on_llm_execution(request=request, next_call=self.frontier, api_mode="chat_completions",
                                                turn_id=turn_id, session_id=session, platform="cli", api_call_count=n)
        msg = response.choices[0].message
        brain = str(getattr(response, "model", "")).startswith("hypnos-brain")
        if msg.tool_calls:
            fn = msg.tool_calls[0].function
            return {"tool": fn.name, "args": json.loads(fn.arguments or "{}"), "id": msg.tool_calls[0].id, "brain": brain}
        return {"text": msg.content or "", "brain": brain}

    def run_turn(self, task: Task, user: str, session: str, ts: float, stats: DayStats, prev_user: str) -> bool:
        """One user turn. Returns True when the persona was happy with the action."""
        turn_id = f"{session}:{int(ts)}:{self.rng.randrange(1 << 30):x}"
        self.frontier.task = task
        self._put("turn_start", {"session_id": session, "turn_id": turn_id, "user_message": user, "platform": "cli",
                                 "model": "demo-frontier", "previous_user_message": prev_user, "source": "synthetic"}, ts)
        self.writer.flush(10)
        block = self.injector.block(self.db_path)
        user_wire = user + (f"\n\n{block}" if block else "")
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_wire}]
        first = self._call(messages, session, turn_id, 1)
        stats.calls += 1
        stats.brain_calls += int(first["brain"])
        turn_messages = []
        happy = False
        if first.get("tool"):
            command = str((first.get("args") or {}).get("command", ""))
            happy = self.persona.follows(task, first)
            call_id = first.get("id") or f"call_{turn_id}"
            needs_approval = any(p in command for p in APPROVAL_PATTERNS)
            if needs_approval:
                self._put("approval", {"tool_call_id": call_id, "turn_id": turn_id, "choice": "once" if happy else "deny"},
                          ts + 1)
            if needs_approval and not happy:
                stats.denials += 1
                result = json.dumps({"output": "", "exit_code": -1, "status": "blocked",
                                     "error": "BLOCKED: User denied this command. Do NOT retry this command."})
            else:
                result = json.dumps({"output": task.output if happy else "ok", "exit_code": 0, "error": None})
            turn_messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
                              "function": {"name": first["tool"], "arguments": json.dumps(first.get("args") or {})}}]},
                             {"role": "tool", "tool_call_id": call_id, "content": result}]
            self._put("tool", {"turn_id": turn_id, "tool_call_id": call_id, "tool_name": first["tool"],
                               "args": first.get("args") or {}, "result": result, "duration_ms": 40.0}, ts + 2)
            second = self._call(messages + turn_messages, session, turn_id, 2)
            stats.calls += 1
            stats.brain_calls += int(second["brain"])
            final = second.get("text") or "Done."
        else:
            final = first.get("text") or "Done."
        turn_messages.append({"role": "assistant", "content": final})
        self._put("turn_end", {"turn_id": turn_id, "assistant_response": final, "turn_messages": turn_messages}, ts + 3)
        self._put("turn_close", {"turn_id": turn_id, "completed": True}, ts + 3)
        self._settle()
        stats.turns += 1
        return happy

    def run_day(self, day: int, start_ts: float) -> DayStats:
        stats = DayStats()
        ts = start_ts
        for c, chain in enumerate(day_tasks(day, self.rng)):
            session = f"demo-d{day}-c{c}"
            prev = ""
            for task in chain:
                self.frontier.corrected = False
                happy = self.run_turn(task, task.user, session, ts, stats, prev)
                prev = task.user
                ts += 600
                if not happy and task.habit:
                    stats.corrections += 1
                    self.frontier.corrected = True
                    correction = self.persona.correction(task.habit)
                    self.run_turn(task, correction, session, ts, stats, prev)
                    prev = correction
                    ts += 600
        return stats

    def run(self) -> list[dict]:
        summary = []
        base = time.time() - self.days * 86400
        for day in range(1, self.days + 1):
            day_start = base + (day - 1) * 86400 + 9 * 3600
            stats = self.run_day(day, day_start)
            conn = dbmod.open_db(self.db_path)
            from .signals import sleep_pressure
            pressure = sleep_pressure(conn).percent
            conn.close()
            self.out(f"Day {day}: {stats.turns} turns, {stats.corrections} corrections, {stats.denials} denials, "
                     f"{stats.brain_calls}/{stats.calls} calls answered by the brain, sleep pressure {pressure:.0f}%")
            summary.append({"day": day, **stats.__dict__, "pressure": pressure})
            if day == self.days:
                break
            self.out(f"Night {day}: sleeping...")
            report = self.night_factory(day) if self.night_factory else {}
            verdict = "promoted" if report.get("promoted") else "bad night"
            self.out(f"Night {day}: {verdict} brain v{report.get('version')}, "
                     f"{len(report.get('habits_consolidated') or [])} habits consolidated, "
                     f"{report.get('nightmares_resolved', 0)}/{report.get('nightmares', 0)} nightmares resolved")
            if report.get("promoted") and self.start_server:
                self.start_server(report)
            self.router.invalidate()
        return summary


def has_real_data(db_path: str) -> bool:
    conn = dbmod.open_db(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM episodes WHERE source != 'synthetic'").fetchone()[0] > 0
    finally:
        conn.close()
