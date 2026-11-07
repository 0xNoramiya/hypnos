import json
from pathlib import Path

import pytest
from helpers import Turn

from hypnos import db as dbmod
from hypnos.capture import Event, Recorder
from hypnos.exam import decide, grade, wilson_lower
from hypnos.night import Night, NightConfig, NightError, request_cancel, schedule
from hypnos.report import build_report, render_report

FAKE = str(Path(__file__).parent / "fixtures" / "fake_trainer.py")
PKGS = ["lodash", "zod", "axios", "dayjs", "chalk", "express", "nanoid", "prettier", "eslint", "vitest", "react", "vue"]


def populate(db_path):
    rec = Recorder(dbmod.open_db(db_path))

    def sink(kind, data):
        rec.apply(Event(kind, "", data))

    t1 = Turn(sink, "s1", "add lodash")
    t1.terminal("npm install lodash", output="added 1 package")
    t1.end("Installed.")
    t2 = Turn(sink, "s1", "no, use pnpm instead", previous_user_message="add lodash")
    t2.terminal("pnpm add lodash", output="+ lodash")
    t2.end("Done with pnpm.")
    for i, pkg in enumerate(PKGS):
        t = Turn(sink, f"s{i + 2}", f"add {pkg}")
        t.terminal(f"pnpm add {pkg}", output=f"+ {pkg}")
        t.end(f"Added {pkg}.")
    return rec.conn


def make_night(tmp_path, **cfg):
    db_path = str(tmp_path / "data.db")
    populate(db_path)
    config = NightConfig(trainer_python=FAKE, cycles=3, max_minutes=5, **cfg)
    lines = []
    return Night(db_path, tmp_path, config, log=lines.append), lines


def test_schedule_shifts_from_n3_to_rem():
    phases = schedule(4, 100, 100)
    n3 = [p["steps"] for p in phases if p["stage"] == "N3"]
    rem = [p["steps"] for p in phases if p["stage"] == "REM"]
    assert sum(n3) == 100 and sum(rem) == 100
    assert n3 == sorted(n3, reverse=True) and rem == sorted(rem)
    assert n3[0] > rem[0] and rem[-1] > n3[-1]


def test_wilson_lower_bound():
    assert wilson_lower(0, 0) == 0
    assert 0.55 < wilson_lower(9, 10) < 0.6
    assert wilson_lower(90, 100) > wilson_lower(9, 10)


def test_night_promotes_a_better_brain(tmp_path):
    night, lines = make_night(tmp_path)
    report = night.run()
    conn = night.conn
    brain = conn.execute("SELECT * FROM brains").fetchone()
    assert brain["version"] == 1 and brain["status"] == "promoted"
    assert brain["manifest_hash"] and len(json.loads(brain["manifest_json"])["episodes"]) > 0
    exam = json.loads(brain["exam_json"])
    assert exam["candidate"]["probe_pass"] == 1.0 and exam["baseline"]["probe_pass"] == 0.0
    assert "packages" in exam["decision"]["families"]
    stages = [r["stage"] for r in conn.execute("SELECT stage FROM night_events ORDER BY id")]
    for stage in ("WAKE", "N1", "N2", "N3", "REM"):
        assert stage in stages
    assert stages[0] == "WAKE" and stages[-1] == "WAKE"
    consolidated = conn.execute("SELECT consolidated FROM habit_results WHERE brain_version = 1").fetchone()
    assert consolidated["consolidated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE consolidated_night IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM nights").fetchone()[0] == "done"
    text = render_report(report)
    assert "PROMOTED" in text and "Use pnpm" in text and chr(0x2014) not in text


def test_second_night_trains_from_parent(tmp_path):
    night, _ = make_night(tmp_path)
    night.run()
    night2 = Night(night.db_path, tmp_path, night.cfg)
    night2.run()
    job = json.loads((tmp_path / "nights" / "2" / "job.json").read_text())
    assert job["parent_adapter"].endswith("brains/v1")
    rows = night2.conn.execute("SELECT version, parent_version, status FROM brains ORDER BY version").fetchall()
    assert rows[1]["parent_version"] == 1


def test_dpo_skips_text_rejections_but_sft_keeps_the_action(tmp_path):
    night, _ = make_night(tmp_path)
    night.conn.execute(
        "INSERT INTO pairs(created_at, source, chosen_json, rejected_json, confidence, gold, detail) VALUES(?,?,?,?,?,?,?)",
        (0, "dream", json.dumps({"tool": "terminal", "args": {"command": "pnpm add zod"}}), json.dumps({"text": "Done."}),
         0.9, 1, json.dumps({"user": "Pull in zod."})))
    data = night.build_data(tmp_path, None)
    assert data["dpo"] and all(r["rejected"].get("tool_calls") for r in data["dpo"])
    assert any("Pull in zod." in json.dumps(r["messages"]) and "pnpm add zod" in json.dumps(r["target"]) for r in data["sft"])


def test_bad_night_keeps_previous_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPNOS_FAKE_POLICY", "garbage")
    night, _ = make_night(tmp_path)
    report = night.run()
    brain = night.conn.execute("SELECT status FROM brains").fetchone()
    assert brain["status"] == "rejected"
    assert night.conn.execute("SELECT status FROM nights").fetchone()[0] == "bad_night"
    assert not report["promoted"] and report["reasons"]
    assert "BAD NIGHT" in render_report(report)
    conn = night.conn
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE consolidated_night IS NULL").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM pairs WHERE night_id IS NOT NULL").fetchone()[0] == 0
    # A habit that passed its probes on a rejected brain is still not in the weights.
    exam = json.loads(conn.execute("SELECT exam_json FROM brains").fetchone()[0])
    exam["decision"]["consolidated"] = [1]
    conn.execute("UPDATE brains SET exam_json = ?", (json.dumps(exam),))
    assert build_report(conn, 1, 1)["habits_consolidated"] == []


def test_worker_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPNOS_FAKE_FAIL", "1")
    night, _ = make_night(tmp_path)
    with pytest.raises(NightError, match="simulated failure"):
        night.run()
    assert night.conn.execute("SELECT status FROM nights").fetchone()[0] == "failed"
    assert night.conn.execute("SELECT COUNT(*) FROM brains").fetchone()[0] == 0


def test_missing_trainer_prints_install_command(tmp_path):
    night, _ = make_night(tmp_path)
    night.cfg.trainer_python = str(tmp_path / "nope" / "python")
    with pytest.raises(NightError, match="uv pip install"):
        night.run()


def test_nothing_to_learn(tmp_path):
    db_path = str(tmp_path / "data.db")
    dbmod.open_db(db_path)
    report = Night(db_path, tmp_path, NightConfig(trainer_python=FAKE)).run()
    assert report["status"] == "skipped"


def test_only_one_night_at_a_time(tmp_path):
    night, _ = make_night(tmp_path)
    night._lock()
    try:
        other = Night(night.db_path, tmp_path, night.cfg)
        with pytest.raises(NightError, match="already asleep"):
            other.run()
    finally:
        night._unlock()


def test_request_cancel(tmp_path):
    conn = dbmod.open_db(tmp_path / "d.db")
    assert not request_cancel(conn)
    conn.execute("INSERT INTO nights(started_at, status) VALUES(0, 'running')")
    assert request_cancel(conn)


def test_decide_requires_beating_baseline():
    items = [{"id": f"i{i}", "kind": "heldout", "family": "packages",
              "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {
                  "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}],
              "target": {"tool_calls": [{"name": "terminal", "arguments": {"command": f"pnpm add p{i}"}}]}} for i in range(10)]
    good = [{"id": f"i{i}", "text": f'<tool_call>{{"name":"terminal","arguments":{{"command":"pnpm add p{i}"}}}}</tool_call>'}
            for i in range(10)]
    bad = [{"id": f"i{i}", "text": "no idea"} for i in range(10)]
    cand, _ = grade(items, good)
    base, _ = grade(items, bad)
    assert decide(cand, base).promote
    assert not decide(base, cand).promote
    assert not decide(cand, cand).promote


def test_retention_purges_old_real_turns(tmp_path):
    night, _ = make_night(tmp_path, retention_days=30)
    night.conn.execute("UPDATE episodes SET started_at = 0 WHERE id = 1")
    night.night_id = None
    assert night.apply_retention() == 1
    assert night.conn.execute("SELECT COUNT(*) FROM episodes WHERE id = 1").fetchone()[0] == 0


def test_teacher_budget_takes_the_lower_cap():
    from hypnos.sleepcmd import teacher_budget
    assert teacher_budget({"teacher_budget_tokens": 60000, "teacher_usd_per_mtok": 3.0, "night_max_usd": 0.06}) == 20000
    assert teacher_budget({"teacher_budget_tokens": 1000, "teacher_usd_per_mtok": 3.0, "night_max_usd": 5}) == 1000


def test_text_answers_are_balanced_against_tool_calls():
    import random

    from hypnos.night import balance_text_answers
    tool = {"id": "a", "target": {"tool_calls": [{"name": "terminal", "arguments": {}}]}}
    text = {"id": "b", "target": {"content": "Done."}}
    rows = balance_text_answers([dict(tool, id=f"t{i}") for i in range(10)] + [text], random.Random(0))
    texts = [r for r in rows if not r["target"].get("tool_calls")]
    assert len(texts) == 5 and len({r["id"] for r in texts}) == 5
    assert balance_text_answers([tool], random.Random(0)) == [tool]
