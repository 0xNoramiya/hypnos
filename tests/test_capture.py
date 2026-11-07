import json
import time

import pytest
from helpers import Turn

from hypnos import db as dbmod
from hypnos.capture import Capture, Event, Recorder, Writer, strip_directives
from hypnos.signals import sleep_pressure


@pytest.fixture
def rec(tmp_path):
    return Recorder(dbmod.open_db(tmp_path / "data.db"))


@pytest.fixture
def sink(rec):
    def _sink(kind, data):
        rec.apply(Event(kind, "", data))
    return _sink


def rows(rec, sql, *args):
    return [dict(r) for r in rec.conn.execute(sql, args).fetchall()]


def test_turn_creates_episode_and_steps(rec, sink):
    t = Turn(sink, "s1", "install lodash")
    t.terminal("pnpm add lodash", output="+ lodash 4.17.21")
    t.end("Installed lodash.")
    ep = rows(rec, "SELECT * FROM episodes")[0]
    assert ep["status"] == "ok" and ep["turn_index"] == 1 and ep["user_message"] == "install lodash"
    steps = rows(rec, "SELECT * FROM steps ORDER BY step_index")
    assert [s["kind"] for s in steps] == ["tool", "text"]
    assert json.loads(steps[0]["args_json"]) == {"command": "pnpm add lodash"}
    assert steps[0]["outcome"] == 1 and steps[0]["exit_code"] == 0


def test_secrets_are_scrubbed_before_persisting(rec, sink):
    t = Turn(sink, "s1", "use my key sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345 and mail jane@example.com")
    t.terminal("export API_KEY=abcdef123456 && curl https://api.x.dev", output="token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    t.end("Used AKIAIOSFODNN7EXAMPLE")
    dump = json.dumps([rows(rec, f"SELECT * FROM {t}") for t in ("episodes", "steps", "signals")])
    for secret in ("sk-ant-api03", "jane@example.com", "abcdef123456", "ghp_abc", "AKIAIOSFODNN7EXAMPLE"):
        assert secret not in dump


def test_denial_then_correction_makes_gold_pair(rec, sink):
    t1 = Turn(sink, "s1", "clean up the old build output")
    t1.terminal("rm -rf ../app-backup/build", approval="deny")
    t1.end("Okay, I will not run that.")
    t2 = Turn(sink, "s1", "no, only delete ./build inside the workdir", previous_user_message="clean up the old build output")
    t2.terminal("rm -rf ./build", approval="once")
    t2.end("Deleted ./build.")
    pairs = rows(rec, "SELECT * FROM pairs")
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["source"] == "denial" and pair["gold"] == 1
    assert json.loads(pair["rejected_json"])["args"]["command"] == "rm -rf ../app-backup/build"
    assert json.loads(pair["chosen_json"])["args"]["command"] == "rm -rf ./build"
    kinds = {r["kind"] for r in rows(rec, "SELECT kind FROM signals")}
    assert {"approval_denied", "correction", "approval_granted", "gold_pair"} <= kinds


def test_correction_without_approval_pairs_npm_to_pnpm(rec, sink):
    t1 = Turn(sink, "s1", "add lodash")
    t1.terminal("npm install lodash", output="added 1 package")
    t1.end()
    t2 = Turn(sink, "s1", "no, use pnpm instead", previous_user_message="add lodash")
    t2.terminal("pnpm add lodash", output="+ lodash")
    t2.end()
    pairs = rows(rec, "SELECT * FROM pairs")
    assert len(pairs) == 1
    assert pairs[0]["source"] == "correction"
    assert json.loads(pairs[0]["rejected_json"])["args"]["command"] == "npm install lodash"
    assert json.loads(pairs[0]["chosen_json"])["args"]["command"] == "pnpm add lodash"
    assert pairs[0]["confidence"] >= 0.75


def test_unrelated_followup_is_not_paired(rec, sink):
    t1 = Turn(sink, "s1", "delete the cache")
    t1.terminal("rm -rf ../shared-cache", approval="deny")
    t1.end()
    t2 = Turn(sink, "s1", "never mind, show me the git log")
    t2.terminal("git log --oneline -5", output="abc fix")
    t2.end()
    assert rows(rec, "SELECT * FROM pairs") == []


def test_approval_before_or_after_tool_result(rec, sink):
    t = Turn(sink, "s1", "push it")
    call = t.terminal("git push origin feat/x", output="ok")
    sink("approval", {"tool_call_id": call, "turn_id": t.turn_id, "choice": "once"})
    t.end()
    step = rows(rec, "SELECT approval FROM steps WHERE tool_call_id = ?", call)[0]
    assert step["approval"] == "approved"


def test_test_runner_signals(rec, sink):
    t = Turn(sink, "s1", "run tests")
    t.terminal("pnpm test", output="Tests:       1 failed, 3 passed, 4 total")
    t.terminal("pytest -q", output="===== 5 passed in 0.2s =====")
    t.end()
    kinds = sorted(r["kind"] for r in rows(rec, "SELECT kind FROM signals"))
    assert kinds == ["test_fail", "test_pass"]


def test_unknown_turn_events_are_dropped(rec, sink):
    sink("tool", {"turn_id": "never-opened", "tool_call_id": "c1", "tool_name": "terminal",
                  "args": {"command": "ls"}, "result": "{}"})
    assert rows(rec, "SELECT * FROM steps") == []


def test_purge_and_mark(rec, sink):
    t = Turn(sink, "s1", "hello")
    t.end()
    sink("mark", {"good": False, "session_id": "s1", "note": "bad answer"})
    assert rows(rec, "SELECT kind FROM signals")[0]["kind"] == "mark_bad"
    sink("purge", {"session_ids": ["s1"]})
    for table in ("episodes", "steps", "signals", "pairs", "sessions"):
        assert rows(rec, f"SELECT * FROM {table}") == []


def test_sleep_pressure_rises_with_corrections(rec, sink):
    assert sleep_pressure(rec.conn).percent == 0
    t1 = Turn(sink, "s1", "add lodash")
    t1.terminal("npm install lodash", output="ok")
    t1.end()
    low = sleep_pressure(rec.conn).percent
    t2 = Turn(sink, "s1", "no, use pnpm instead", previous_user_message="add lodash")
    t2.terminal("pnpm add lodash", output="ok")
    t2.end()
    high = sleep_pressure(rec.conn)
    assert high.percent > low > 0
    assert high.counts["correction"] == 1


def test_directive_is_stripped():
    assert strip_directives('Done.\n\n::hypnos{t="abc"}') == "Done."
    assert strip_directives("plain") == "plain"


class _Settings:
    def __init__(self, **kw):
        self.values = {"enabled": True, "exclude_platforms": ["cron"], **kw}

    def __call__(self):
        return self.values


def _capture(tmp_path, **settings):
    writer = Writer()
    return Capture(db_path=lambda: str(tmp_path / "data.db"), settings=_Settings(**settings), writer=writer), writer


def test_capture_end_to_end_through_writer(tmp_path):
    cap, writer = _capture(tmp_path)
    history = [{"role": "user", "content": "add lodash"}]
    cap.on_pre_llm_call(session_id="s", turn_id="t1", user_message="add lodash", conversation_history=history,
                        platform="cli", model="m")
    cap.on_post_tool_call(turn_id="t1", tool_call_id="c1", tool_name="terminal", args={"command": "pnpm add lodash"},
                          result=json.dumps({"output": "ok", "exit_code": 0, "error": None}), status="ok")
    cap.on_post_llm_call(turn_id="t1", assistant_response="done", conversation_history=history + [
        {"role": "assistant", "content": "done"}])
    cap.on_session_end(turn_id="t1", completed=True)
    assert writer.flush(5)
    conn = dbmod.connect(tmp_path / "data.db")
    assert conn.execute("SELECT status FROM episodes").fetchone()[0] == "ok"
    assert writer.errors == 0


def test_capture_respects_off_switch_and_platforms(tmp_path):
    cap, writer = _capture(tmp_path, enabled=False)
    cap.on_pre_llm_call(session_id="s", turn_id="t1", user_message="x", platform="cli")
    assert writer.queue.qsize() == 0
    cap2, writer2 = _capture(tmp_path)
    cap2.on_pre_llm_call(session_id="s", turn_id="t1", user_message="x", platform="cron")
    cap2.on_pre_llm_call(session_id="s", turn_id="t2", user_message="x", platform="cli", parent_session_id="p")
    assert writer2.queue.qsize() == 0


def test_hooks_never_raise(tmp_path):
    def broken():
        raise RuntimeError("settings exploded")
    cap = Capture(db_path=lambda: str(tmp_path / "x.db"), settings=broken, writer=Writer())
    for name, fn in cap.hooks().items():
        assert fn(garbage=object()) is None, name


def test_hook_latency_p95_under_5ms(tmp_path):
    cap, writer = _capture(tmp_path)
    history = [{"role": "user", "content": "x" * 2000}] * 40
    timings = []
    for i in range(400):
        start = time.perf_counter()
        cap.on_pre_llm_call(session_id="s", turn_id=f"t{i}", user_message="add lodash please",
                            conversation_history=history, platform="cli", model="m")
        cap.on_post_tool_call(turn_id=f"t{i}", tool_call_id=f"c{i}", tool_name="terminal",
                              args={"command": "pnpm add lodash"}, result='{"output":"' + "y" * 4000 + '","exit_code":0}')
        cap.on_post_llm_call(turn_id=f"t{i}", assistant_response="done", conversation_history=history)
        timings.append((time.perf_counter() - start) / 3)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95)]
    assert p95 < 0.005, f"p95 {p95 * 1000:.2f} ms"
    assert writer.flush(30)


def test_correction_creates_habit_and_next_turn_gets_habit_block(tmp_path):
    cap, writer = _capture(tmp_path)
    ok = json.dumps({"output": "ok", "exit_code": 0, "error": None})
    history = [{"role": "user", "content": "add lodash"}]
    assert cap.on_pre_llm_call(session_id="s", turn_id="t1", user_message="add lodash",
                               conversation_history=history, platform="cli") is None
    cap.on_post_tool_call(turn_id="t1", tool_call_id="c1", tool_name="terminal",
                          args={"command": "npm install lodash"}, result=ok)
    cap.on_post_llm_call(turn_id="t1", assistant_response="done", conversation_history=history)
    history2 = history + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "no, use pnpm instead"}]
    cap.on_pre_llm_call(session_id="s", turn_id="t2", user_message="no, use pnpm instead",
                        conversation_history=history2, platform="cli")
    cap.on_post_tool_call(turn_id="t2", tool_call_id="c2", tool_name="terminal",
                          args={"command": "pnpm add lodash"}, result=ok)
    cap.on_post_llm_call(turn_id="t2", assistant_response="done", conversation_history=history2)
    assert writer.flush(5)
    block = cap.on_pre_llm_call(session_id="s", turn_id="t3", user_message="add zod", platform="cli")
    assert block and "Use pnpm for packages and scripts, never npm" in block
    cap._settings.values["inject_habits"] = False
    assert cap.on_pre_llm_call(session_id="s", turn_id="t4", user_message="add zod", platform="cli") is None
