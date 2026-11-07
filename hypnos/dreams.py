"""REM: dream up variations of the day's mistakes, practise them, and keep what was verified.

For every habit the brain should hold, a teacher writes K new task variants and the correct next action.
The teacher is the user's own model through ``ctx.llm`` (budget-capped), or a deterministic scripted
teacher for the synthetic demo, tests, and setups without a model.

Each correct action is verified before it is used: by the habit's deterministic checker, and, when Docker
is available, by running it in the sandbox against a fixture project. The current brain then attempts
every variant without the habit text. A variant it fails is a nightmare; failing attempts become the
rejected side of DPO pairs. After training, the waking exam re-runs the dreams; a nightmare the new brain
passes is resolved.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import datasets as ds
from . import db as dbmod
from . import sandbox
from .brainfmt import brain_tools, build_messages, parse_output
from .probes import check

EXECUTABLE_KINDS = {"safe_delete", "no_push_main"}


def habit_kind(key: str) -> str:
    if key.startswith("pkg_manager:"):
        return "pkg_manager"
    if key.startswith("safe_delete:"):
        return "safe_delete"
    return {"git:no_push_main": "no_push_main", "git:conventional_commits": "conventional",
            "git:tests_before_commit": "tests_first"}.get(key, key.split(":", 1)[0])


@dataclass
class Variant:
    user: str
    chosen: str
    history: list[dict] = field(default_factory=list)
    violation: str = ""


# --- scripted teacher ---------------------------------------------------------------------------------

_PKGS = ["axios", "zod", "dayjs", "chalk", "nanoid", "express", "uuid", "yaml", "commander", "ora", "got", "ky"]
_PYPKGS = ["httpx", "rich", "typer", "pydantic", "orjson", "tenacity", "structlog", "click"]
_DIRS = ["build", "dist", "coverage", "logs", ".cache", "tmp", "out"]
_TOPICS = ["search box", "retry policy", "auth flow", "export button", "rate limiter", "dark mode", "csv parser",
           "webhook handler"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def scripted_variants(key: str, k: int, rng: random.Random) -> list[Variant]:
    kind = habit_kind(key)
    out: list[Variant] = []
    if kind == "pkg_manager":
        pm = key.split(":", 1)[1]
        node = pm in ("pnpm", "npm", "yarn", "bun")
        bad = "npm install" if pm != "npm" else "yarn add"
        if not node:
            bad = "pip install" if pm != "pip" else "uv add"
        pkgs = _PKGS if node else _PYPKGS
        add = f"{pm} add" if pm not in ("npm", "pip") else f"{pm} install"
        dev = f"{pm} add -D" if node else f"{pm} add --dev"
        forms = [("Pull in {a} for the API.", add + " {a}", bad + " {a}"),
                 ("I need {a} in the frontend, set it up.", add + " {a}", bad + " {a}"),
                 ("Get {a} installed please.", add + " {a}", bad + " {a}"),
                 ("Bring in {a} and {b} for the CLI.", add + " {a} {b}", bad + " {a} {b}"),
                 ("Add {a} as a dev dependency for the tests.", dev + " {a}", bad + " -D {a}"),
                 ("We should use {a} here, add it.", add + " {a}", bad + " {a}")]
        for user, chosen, violation in rng.sample(forms, min(k, len(forms))):
            a, b = rng.sample(pkgs, 2)
            out.append(Variant(user.format(a=a, b=b), chosen.format(a=a, b=b), [], violation.format(a=a, b=b)))
    elif kind == "safe_delete":
        forms = ["Clear out the {d} folder.", "{d} is stale, remove it.", "Delete {d} before we rebuild.",
                 "Throw away the {d} directory.", "Empty {d}, it is huge.", "Remove the generated {d} folder."]
        for user in rng.sample(forms, min(k, len(forms))):
            d = rng.choice(_DIRS)
            out.append(Variant(user.format(d=d), f"rm -rf ./{d}", [], f"rm -rf ../app-backup/{d}"))
    elif kind == "no_push_main":
        history = [{"tool": "terminal", "args": {"command": "git status -sb"}, "result": "## main...origin/main [ahead 1]"}]
        forms = ["Push the {t} changes.", "Send the {t} work to origin.", "Share the {t} commit with the team.",
                 "Get the {t} up on GitHub.", "Push it, the {t} is done.", "Upload the {t} work."]
        for user in rng.sample(forms, min(k, len(forms))):
            t = rng.choice(_TOPICS)
            branch = f"feat/{_slug(t)}"
            out.append(Variant(user.format(t=t), f"git checkout -b {branch} && git push -u origin {branch}", history,
                               "git push origin main"))
    elif kind == "conventional":
        history = [{"tool": "terminal", "args": {"command": "pnpm test"}, "result": "Tests  9 passed (9)"}]
        forms = ["Commit the {t} work.", "Make a commit for the {t}.", "Commit what we have on the {t}.",
                 "Save the {t} in a commit.", "Commit the {t} fix.", "Commit the new {t}."]
        for user in rng.sample(forms, min(k, len(forms))):
            t = rng.choice(_TOPICS)
            prefix = "fix" if "fix" in user else "feat"
            out.append(Variant(user.format(t=t), f'git commit -am "{prefix}({_slug(t).split("-")[0]}): {t}"', history,
                               f'git commit -am "{t} stuff"'))
    elif kind == "tests_first":
        history = [{"tool": "terminal", "args": {"command": "git status -s"}, "result": " M src/app.ts"}]
        forms = ["Commit the {t} work.", "Let's commit the {t}.", "Commit this {t} change.", "Wrap the {t} in a commit.",
                 "Commit and move on from the {t}.", "Check in the {t}."]
        for user in rng.sample(forms, min(k, len(forms))):
            t = rng.choice(_TOPICS)
            out.append(Variant(user.format(t=t), "pnpm test", history, f'git commit -am "feat: {t}"'))
    return out


class ScriptedTeacher:
    name = "scripted"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        self.tokens = 0

    def variants(self, habit: dict, k: int) -> list[Variant]:
        return scripted_variants(habit["key"], k, self.rng)


class LLMTeacher:
    """Uses the user's model through ``ctx.llm``. Stops when the night's token budget is spent."""

    name = "llm"
    SCHEMA = {"type": "object", "properties": {"variants": {"type": "array", "items": {
        "type": "object", "properties": {"user": {"type": "string"}, "command": {"type": "string"},
                                         "wrong_command": {"type": "string"}},
        "required": ["user", "command"]}}}, "required": ["variants"]}

    def __init__(self, llm: Any, budget_tokens: int = 60000, fallback: ScriptedTeacher | None = None):
        self.llm = llm
        self.budget = budget_tokens
        self.tokens = 0
        self.fallback = fallback or ScriptedTeacher()
        self.failed = ""

    def variants(self, habit: dict, k: int) -> list[Variant]:
        if self.failed or self.tokens >= self.budget:
            return self.fallback.variants(habit, k)
        evidence = habit.get("evidence_text") or ""
        instructions = (
            "You help a small local model practise one working habit of its user, so it does the right thing "
            "without being reminded. Write varied, realistic requests a developer would type where this habit "
            "decides the next shell command, plus that correct command, and the tempting wrong command. "
            "Keep each request under 15 words. Commands must be a single line.")
        prompt = (f"Habit: {habit['statement']}\n{evidence}\nWrite {k} variants as JSON "
                  '{"variants": [{"user": "...", "command": "...", "wrong_command": "..."}]}.')
        try:
            result = self.llm.complete_structured(instructions=instructions, input=[{"type": "text", "text": prompt}],
                                                  json_schema=self.SCHEMA, schema_name="hypnos.dream_variants",
                                                  purpose="hypnos.rem_dreams", task="hypnos_teacher",
                                                  temperature=0.8, max_tokens=900)
        except Exception as exc:
            self.failed = f"{type(exc).__name__}: {exc}"[:200]
            return self.fallback.variants(habit, k)
        self.tokens += int(getattr(getattr(result, "usage", None), "total_tokens", 0) or 0)
        parsed = getattr(result, "parsed", None) or {}
        out = []
        for item in (parsed.get("variants") or [])[:k]:
            if isinstance(item, dict) and item.get("user") and item.get("command"):
                out.append(Variant(str(item["user"])[:200], str(item["command"])[:300], [],
                                   str(item.get("wrong_command") or "")[:300]))
        return out or self.fallback.variants(habit, k)


# --- the dreamer ----------------------------------------------------------------------------------------

class Dreamer:
    def __init__(self, teacher: Any, *, k: int = 4, max_habits: int = 8, use_sandbox: bool = True,
                 generate: Callable[[list[dict], Path], list[dict]] | None = None, regression_cap: int = 40):
        self.teacher = teacher
        self.k = k
        self.max_habits = max_habits
        self.use_sandbox = use_sandbox
        self._generate = generate
        self.regression_cap = regression_cap
        self.mode = "lucid"
        self.mode_reason = ""
        self.night = None

    def _verify(self, kind: str, checker: dict, command: str) -> tuple[bool, str]:
        action = {"tool": "terminal", "args": {"command": command}}
        verdict = check(checker, action)
        if not verdict.passed:
            return False, f"pattern: {verdict.reason}"
        if self.mode == "sandbox" and kind in EXECUTABLE_KINDS:
            ok, why = sandbox.outcome_ok(kind, sandbox.run(command))
            if ok is False:
                return False, f"sandbox: {why}"
            return True, "sandbox"
        return True, "pattern"

    def prepare(self, night: Any) -> None:
        """N2: write tonight's dreams, verify them, let the current brain attempt them, and make pairs."""
        self.night = night
        conn = night.conn
        if self.use_sandbox:
            usable, reason = sandbox.available()
            self.mode, self.mode_reason = ("sandbox", "") if usable else ("lucid", reason)
        else:
            self.mode, self.mode_reason = "lucid", "sandbox disabled"
        night.event("N2", "dream_mode", mode=self.mode, reason=self.mode_reason, teacher=self.teacher.name)
        habits = conn.execute(
            "SELECT h.*, (SELECT checker_json FROM probes p WHERE p.habit_id = h.id AND p.kind = 'deterministic' "
            "LIMIT 1) AS checker FROM habits h WHERE h.status = 'active' AND h.excluded = 0 ORDER BY h.id").fetchall()
        rows = []
        for habit in [h for h in habits if h["checker"]][: self.max_habits]:
            checker = dbmod.loads(habit["checker"], {})
            kind = habit_kind(habit["key"])
            evidence = conn.execute("SELECT rejected_json, chosen_json FROM pairs WHERE habit_id = ? LIMIT 1",
                                    (habit["id"],)).fetchone()
            info = dict(habit)
            if evidence:
                info["evidence_text"] = (f"Example wrong action: {evidence['rejected_json']}\n"
                                         f"Example right action: {evidence['chosen_json']}")
            for variant in self.teacher.variants(info, self.k):
                ok, verified_by = self._verify(kind, checker, variant.chosen)
                if not ok:
                    night.event("N2", "dream_discarded", habit=habit["statement"][:80], why=verified_by)
                    continue
                cur = conn.execute(
                    "INSERT INTO dreams(night_id, habit_id, family, kind, prompt_json, checker_json, chosen_json, "
                    "teacher, verified_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (night.night_id, habit["id"], habit["family"], kind,
                     dbmod.dumps({"user": variant.user, "history": variant.history}), dbmod.dumps(checker),
                     dbmod.dumps({"tool": "terminal", "args": {"command": variant.chosen}}), self.teacher.name,
                     verified_by, time.time()))
                rows.append((cur.lastrowid, habit, variant, kind, checker))
        night.event("N2", "dreams_written", dreams=len(rows), teacher_tokens=getattr(self.teacher, "tokens", 0))
        if not rows:
            return
        prompts = [{"id": f"dream:{dream_id}", "messages": build_messages(v.user, v.history, []),
                    "tools": brain_tools(None, habit["family"] or "shell")} for dream_id, habit, v, _, _ in rows]
        outputs = {o["id"]: o for o in (self._generate(prompts, night) if self._generate else [])}
        nightmares = 0
        for dream_id, habit, variant, kind, checker in rows:
            out = outputs.get(f"dream:{dream_id}")
            attempt = parse_output(out["text"]) if out else {}
            passed = bool(out) and bool(check(checker, attempt).passed)
            if passed and self.mode == "sandbox" and kind in EXECUTABLE_KINDS and attempt.get("tool") == "terminal":
                ok, _ = sandbox.outcome_ok(kind, sandbox.run(str((attempt.get("args") or {}).get("command", ""))))
                passed = ok is not False
            nightmare = bool(out) and not passed
            conn.execute("UPDATE dreams SET attempt_json = ?, attempt_passed = ?, nightmare = ? WHERE id = ?",
                         (dbmod.dumps(attempt), int(passed) if out else None, int(nightmare), dream_id))
            if nightmare:
                nightmares += 1
                shown = (attempt.get("args") or {}).get("command") or attempt.get("text") or "(no answer)"
                night.event("N2", "nightmare", dream=dream_id, habit=habit["statement"][:80], request=variant.user,
                            brain_did=str(shown)[:120])
            rejected = None
            if nightmare and (attempt.get("tool") or (attempt.get("text") or "").strip()):
                rejected = attempt if attempt.get("tool") else {"text": attempt.get("text", "")}
            elif variant.violation:
                rejected = {"tool": "terminal", "args": {"command": variant.violation}}
            if rejected is None:
                continue
            chosen = {"tool": "terminal", "args": {"command": variant.chosen}}
            conn.execute(
                "INSERT INTO pairs(created_at, source, chosen_json, rejected_json, confidence, gold, habit_id, night_id, detail) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), "dream", dbmod.dumps(chosen), dbmod.dumps(rejected), 0.9, 1, habit["id"], night.night_id,
                 json.dumps({"user": variant.user, "history": variant.history, "dream_id": dream_id})))
        night.event("N2", "dreams_rehearsed", dreams=len(rows), nightmares=nightmares)

    def exam_items(self) -> list[dict]:
        """Tonight's dreams plus earlier ones (the regression set), without habit text."""
        if self.night is None:
            return []
        rows = self.night.conn.execute(
            "SELECT * FROM dreams WHERE excluded = 0 ORDER BY (night_id = ?) DESC, id DESC LIMIT ?",
            (self.night.night_id, self.regression_cap)).fetchall()
        items = []
        for row in rows:
            prompt = dbmod.loads(row["prompt_json"], {})
            items.append({"id": f"dream:{row['id']}", "kind": "dream", "habit_id": row["habit_id"], "family": row["family"],
                          "messages": build_messages(prompt.get("user", ""), prompt.get("history"), []),
                          "tools": brain_tools(None, row["family"] or "shell"),
                          "checker": dbmod.loads(row["checker_json"], {})})
        return items

    def after_exam(self, results: list) -> int:
        """Mark nightmares the candidate now passes as resolved. Returns how many were resolved."""
        if self.night is None:
            return 0
        conn = self.night.conn
        resolved = 0
        for res in results:
            if res.kind != "dream" or not res.id.startswith("dream:"):
                continue
            dream_id = int(res.id.split(":", 1)[1])
            row = conn.execute("SELECT nightmare, night_id FROM dreams WHERE id = ?", (dream_id,)).fetchone()
            if row is None or not row["nightmare"] or row["night_id"] != self.night.night_id:
                continue
            conn.execute("UPDATE dreams SET resolved = ? WHERE id = ?", (int(res.passed), dream_id))
            if res.passed:
                resolved += 1
                self.night.event("WAKE", "nightmare_resolved", dream=dream_id)
        return resolved


def worker_generate(python: Path, base_model: str, adapter: str | None, hyper: dict | None = None):
    """A ``generate`` callable for the Dreamer that runs the current brain in the trainer worker."""
    from .workerproc import WorkerProcess

    def run(prompts: list[dict], night: Any) -> list[dict]:
        night_dir = night.data_dir / "nights" / str(night.night_id)
        prompts_path, out_path = night_dir / "dreams_in.jsonl", night_dir / "dreams_out.jsonl"
        ds.write_jsonl(prompts_path, prompts)
        job = {"kind": "generate", "base_model": base_model, "adapter": adapter, "prompts_path": str(prompts_path),
               "out_path": str(out_path), "hyper": hyper or {}}
        job_path = night_dir / "dream_job.json"
        job_path.write_text(json.dumps(job))
        night.event("N2", "rehearsing_dreams", n=len(prompts))
        worker = WorkerProcess(python, job_path, night_dir / "worker.log")
        error = ""
        for ev in worker.events():
            if ev.get("event") == "error":
                error = ev.get("message", "")
        if worker.returncode != 0 or error or not out_path.exists():
            night.event("N2", "rehearsal_failed", reason=error or f"exit {worker.returncode}")
            return []
        return ds.read_jsonl(out_path)

    return run
