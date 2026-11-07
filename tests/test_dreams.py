import json
import random
from types import SimpleNamespace

import pytest
from test_night import FAKE, populate

from hypnos import habits as H
from hypnos import sandbox
from hypnos.dreams import Dreamer, LLMTeacher, ScriptedTeacher, habit_kind, scripted_variants
from hypnos.night import Night, NightConfig
from hypnos.probes import check
from hypnos.report import render_report

TEMPLATES = [H.pkg_manager_habit("pnpm", "t"), H.pkg_manager_habit("uv", "t"), H.safe_delete_habit("t"),
             H.branch_habit("t"), H.conventional_commit_habit("t"), H.tests_first_habit("t")]


def act(command):
    return {"tool": "terminal", "args": {"command": command}}


@pytest.mark.parametrize("spec", TEMPLATES, ids=lambda s: s.key)
def test_scripted_variants_are_correct_and_distinct_from_probes(spec):
    variants = scripted_variants(spec.key, 6, random.Random(1))
    assert len(variants) >= 4
    checker = spec.probes[0].checker
    probe_prompts = {p.prompt["user"] for p in spec.probes}
    for v in variants:
        assert check(checker, act(v.chosen)).passed, v.chosen
        assert not check(checker, act(v.violation)).passed, v.violation
        assert v.user not in probe_prompts


def test_habit_kind():
    assert habit_kind("pkg_manager:pnpm") == "pkg_manager"
    assert habit_kind("git:no_push_main") == "no_push_main"


class FakeLLM:
    def __init__(self, parsed=None, error=None):
        self.parsed, self.error, self.calls = parsed, error, 0

    def complete_structured(self, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(parsed=self.parsed, usage=SimpleNamespace(total_tokens=500))


def test_llm_teacher_and_budget():
    llm = FakeLLM({"variants": [{"user": "Grab zod", "command": "pnpm add zod", "wrong_command": "npm i zod"}]})
    teacher = LLMTeacher(llm, budget_tokens=600)
    habit = {"key": "pkg_manager:pnpm", "statement": "Use pnpm"}
    assert teacher.variants(habit, 3)[0].chosen == "pnpm add zod"
    teacher.variants(habit, 3)
    assert teacher.tokens == 1000 and llm.calls == 2
    teacher.variants(habit, 3)  # over budget: scripted fallback, no third call
    assert llm.calls == 2


def test_llm_teacher_failure_falls_back():
    teacher = LLMTeacher(FakeLLM(error=RuntimeError("no provider")))
    out = teacher.variants({"key": "pkg_manager:pnpm", "statement": "Use pnpm"}, 3)
    assert out and "no provider" in teacher.failed


def _night_with_dreamer(tmp_path, generate=None, use_sandbox=False):
    db_path = str(tmp_path / "data.db")
    populate(db_path)
    dreamer = Dreamer(ScriptedTeacher(seed=3), k=4, use_sandbox=use_sandbox, generate=generate)
    night = Night(db_path, tmp_path, NightConfig(trainer_python=FAKE, cycles=2, max_minutes=5), dreamer=dreamer)
    return night, dreamer


def test_night_dreams_nightmares_and_resolution(tmp_path):
    night, dreamer = _night_with_dreamer(tmp_path)
    report = night.run()
    conn = night.conn
    dreams = conn.execute("SELECT * FROM dreams").fetchall()
    assert len(dreams) == 4
    assert all(d["nightmare"] == 1 for d in dreams)  # the untrained brain used npm
    assert all(d["resolved"] == 1 for d in dreams)  # the candidate passes them after training
    pairs = conn.execute("SELECT * FROM pairs WHERE source = 'dream'").fetchall()
    assert len(pairs) == 4 and all(json.loads(p["rejected_json"])["args"]["command"].startswith("npm ") for p in pairs)
    events = [r["event"] for r in conn.execute("SELECT event FROM night_events")]
    assert events.count("nightmare") == 4 and events.count("nightmare_resolved") == 4
    assert report["nightmares"] == 4 and report["nightmares_resolved"] == 4
    assert report["exam"]["candidate"]["dream_n"] == 4
    text = render_report(report)
    assert "Nightmares         4 of 4 resolved" in text and "lucid" in text
    manifest = json.loads(conn.execute("SELECT manifest_json FROM brains").fetchone()[0])
    assert len(manifest["params"]["dreams"]) == 4
    assert set(manifest["pairs"]) >= {p["id"] for p in pairs}


def test_chatty_nightmare_is_contrasted_with_the_violation(tmp_path):
    def chatty(prompts, night):
        return [{"id": p["id"], "text": "Done.", "mean_logprob": -0.1} for p in prompts]

    night, _ = _night_with_dreamer(tmp_path, generate=chatty)
    night.run()
    rejected = [json.loads(r[0]) for r in night.conn.execute("SELECT rejected_json FROM pairs WHERE source = 'dream'")]
    assert rejected and all(r.get("tool") == "terminal" for r in rejected)
    dpo = [json.loads(line) for line in (tmp_path / "nights" / "1" / "dpo.jsonl").read_text().splitlines()]
    assert dpo and all(r["rejected"].get("tool_calls") for r in dpo)


def test_dreams_are_regression_items_next_night(tmp_path):
    night, dreamer = _night_with_dreamer(tmp_path)
    night.run()
    night2 = Night(night.db_path, tmp_path, night.cfg, dreamer=Dreamer(ScriptedTeacher(seed=4), k=2, use_sandbox=False))
    night2.run()
    items = night2.dreamer.exam_items()
    nights = {night2.conn.execute("SELECT night_id FROM dreams WHERE id = ?", (int(i["id"].split(":")[1]),)).fetchone()[0]
              for i in items}
    assert nights == {1, 2}


@pytest.mark.skipif(not sandbox.available()[0], reason="docker sandbox not available")
def test_sandbox_verifies_and_rejects():
    d = Dreamer(ScriptedTeacher(), use_sandbox=True)
    d.mode = "sandbox"
    checker = H.safe_delete_habit("t").probes[0].checker
    assert d._verify("safe_delete", checker, "rm -rf ./build") == (True, "sandbox")
    branch = H.branch_habit("t").probes[0].checker
    ok, why = d._verify("no_push_main", branch, "git checkout -b feat/x && git push -u origin feat/x")
    assert ok and why == "sandbox"


def test_lucid_mode_without_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: (False, "docker is not installed"))
    night, dreamer = _night_with_dreamer(tmp_path, use_sandbox=True)
    night.run()
    assert dreamer.mode == "lucid"
    verified = {r[0] for r in night.conn.execute("SELECT verified_by FROM dreams")}
    assert verified == {"pattern"}
