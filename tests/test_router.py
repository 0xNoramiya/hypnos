import json
import time

import pytest

from hypnos import db as dbmod
from hypnos.capture import Event, Recorder, Writer
from hypnos.router import Router, brain_view, frontier_action, provider_response, strip_habit_block

TERMINAL = {"type": "function", "function": {
    "name": "terminal", "description": "Run shell commands on the OS. Long description follows.\nMore.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "x"},
                                                    "background": {"type": "boolean"}}, "required": ["command"]}}}
OTHER = {"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object"}}}


def chat_request(user="Install axios.", steps=()):
    messages = [{"role": "system", "content": "You are Hermes. " * 200},
                {"role": "user", "content": user + "\n\nHabits learned by Hypnos (follow them):\n- Use pnpm."}]
    for i, (command, result) in enumerate(steps):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": result})
    return {"model": "frontier", "messages": messages, "tools": [TERMINAL, OTHER]}


class FakeFrontier:
    def __init__(self, command="pnpm add axios"):
        self.calls = 0
        self.command = command

    def __call__(self, request):
        self.calls += 1
        return provider_response({"tool": "terminal", "args": {"command": self.command}}, "chat_completions",
                                 version=0, prompt_tokens=12000, completion_tokens=20)


def brain_reply(command=None, text=None, logprob=-0.05, name="terminal"):
    message = {"role": "assistant", "content": text}
    if command is not None:
        message["tool_calls"] = [{"id": "x", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps({"command": command})}}]
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 180, "completion_tokens": 22},
            "hypnos": {"mean_logprob": logprob}}


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "data.db")
    conn = dbmod.open_db(db_path)
    settings = {"enabled": True, "routing": "auto", "shadow_agree_turns": 2, "confidence_threshold": -0.35,
                "brain_timeout_s": 5, "brain_max_context": 3072, "exclude_platforms": ["cron"]}
    writer = Writer()
    calls = []
    reply = {"value": brain_reply("pnpm add axios")}

    def client(endpoint, body, timeout):
        calls.append(body)
        if isinstance(reply["value"], Exception):
            raise reply["value"]
        return reply["value"]

    router = Router(db_path=lambda: db_path, settings=lambda: settings, writer=writer, brain_client=client,
                    endpoint=lambda s: "http://127.0.0.1:8765/v1", state_ttl=0)
    writer.on_brain_changed = router.invalidate
    return {"conn": conn, "router": router, "writer": writer, "calls": calls, "reply": reply, "settings": settings,
            "db_path": db_path}


def promote(conn, state="shadow", consolidated=True):
    conn.execute("INSERT INTO habits(id, key, statement, created_at, updated_at, family) VALUES(1,'k','Use pnpm, never npm.',0,0,'packages')")
    conn.execute("INSERT INTO habits(id, key, statement, created_at, updated_at, family) VALUES(2,'k2','Write conventional commits.',0,0,'git')")
    conn.execute("INSERT INTO brains(version, base_model, status, families_json, created_at) VALUES(1,'m','promoted',?,0)",
                 (json.dumps({"packages": {"state": state, "agree": 0, "turns": 0}}),))
    conn.execute("INSERT INTO habit_results(habit_id, brain_version, with_text, passes, total, consolidated, created_at) "
                 "VALUES(1,1,0,10,10,?,0)", (int(consolidated),))


def routes(env):
    env["writer"].flush(5)
    return [dict(r) for r in dbmod.connect(env["db_path"]).execute("SELECT * FROM routes ORDER BY id")]


def run(env, request=None, **kw):
    frontier = FakeFrontier()
    out = env["router"].on_llm_execution(request=request or chat_request(), next_call=frontier,
                                         api_mode=kw.get("api_mode", "chat_completions"), turn_id=kw.get("turn_id", "t1"),
                                         session_id="s", platform=kw.get("platform", "cli"), api_call_count=1)
    return out, frontier


def test_no_brain_passes_through_and_records_tools(env):
    out, frontier = run(env)
    assert frontier.calls == 1 and env["calls"] == []
    rows = routes(env)
    assert rows[0]["mode"] == "passthrough" and rows[0]["frontier_prompt_tokens"] == 12000
    assert dbmod.connect(env["db_path"]).execute("SELECT COUNT(*) FROM toolsets").fetchone()[0] == 1


def test_shadow_runs_both_and_logs_agreement(env):
    promote(env["conn"], "shadow")
    out, frontier = run(env)
    assert frontier.calls == 1
    assert frontier_action(out, "chat_completions")["args"]["command"] == "pnpm add axios"
    time.sleep(0.2)
    rows = routes(env)
    assert rows[-1]["mode"] == "shadow" and rows[-1]["agreed"] == 1
    # The brain saw a compact prompt: no Hermes system prompt, no injected block, pruned habit.
    body = env["calls"][0]
    system = body["messages"][0]["content"]
    assert "You are Hermes" not in system and "Use pnpm" not in system and "conventional" in system
    assert body["messages"][1]["content"] == "Install axios."
    assert [t["function"]["name"] for t in body["tools"]] == ["terminal"]


def test_shadow_goes_live_after_agreeing_turns_then_brain_answers(env):
    promote(env["conn"], "shadow")
    for i in range(2):
        run(env, turn_id=f"t{i}")
        time.sleep(0.2)
    routes(env)
    fam = json.loads(env["conn"].execute("SELECT families_json FROM brains").fetchone()[0])["packages"]
    assert fam["state"] == "live"
    out, frontier = run(env, turn_id="t9")
    assert frontier.calls == 0
    assert out.model == "hypnos-brain-v1" and out.choices[0].finish_reason == "tool_calls"
    call = out.choices[0].message.tool_calls[0]
    assert call.function.name == "terminal" and json.loads(call.function.arguments) == {"command": "pnpm add axios"}
    row = routes(env)[-1]
    assert row["mode"] == "brain" and row["tokens_saved"] > 500 and row["pruned_tokens"] > 0


@pytest.mark.parametrize("reply,reason", [
    (brain_reply("pnpm add axios", name="rm_everything"), "not offered"),
    (brain_reply("pnpm add axios", logprob=-2.0), "low confidence"),
    (brain_reply(text="<tool_call>{oops"), "unparseable"),
    (RuntimeError("down"), "brain unavailable"),
])
def test_live_falls_back_to_frontier(env, reply, reason):
    promote(env["conn"], "live")
    env["reply"]["value"] = reply
    out, frontier = run(env)
    assert frontier.calls == 1
    assert out.model == "hypnos-brain-v0"  # the frontier fake's response, untouched
    row = routes(env)[-1]
    assert row["mode"] == "fallback" and reason in row["fallback_reason"]


def test_repeated_fallbacks_demote_family(env):
    promote(env["conn"], "live")
    env["reply"]["value"] = RuntimeError("down")
    for i in range(5):
        run(env, turn_id=f"t{i}")
    routes(env)
    fam = json.loads(env["conn"].execute("SELECT families_json FROM brains").fetchone()[0])["packages"]
    assert fam["state"] == "shadow" and "fallback" in fam["demoted"]


def test_mark_bad_demotes_brain_turn(env):
    promote(env["conn"], "live")
    env["conn"].execute("INSERT INTO episodes(turn_id, session_id, turn_index, started_at, status) VALUES('t1','s',1,0,'ok')")
    run(env, turn_id="t1")
    routes(env)
    rec = Recorder(dbmod.open_db(env["db_path"]))
    rec.apply(Event("mark", "", {"good": False, "session_id": "s"}))
    fam = json.loads(env["conn"].execute("SELECT families_json FROM brains").fetchone()[0])["packages"]
    assert fam["state"] == "shadow"


def test_unmastered_family_and_codex_and_cron_pass_through(env):
    promote(env["conn"], "live")
    out, frontier = run(env, request=chat_request("Explain the router module to me"))
    assert frontier.calls == 1
    out, frontier = run(env, api_mode="codex_responses")
    assert frontier.calls == 1
    out, frontier = run(env, platform="cron")
    assert frontier.calls == 1
    env["settings"]["routing"] = "off"
    out, frontier = run(env)
    assert frontier.calls == 1


def test_shadow_only_never_answers(env):
    promote(env["conn"], "live")
    env["settings"]["routing"] = "shadow_only"
    out, frontier = run(env)
    assert frontier.calls == 1


def test_anthropic_wire(env):
    promote(env["conn"], "live")
    request = {"model": "claude", "system": [{"type": "text", "text": "You are Hermes."}],
               "tools": [{"name": "terminal", "description": "Run", "input_schema": TERMINAL["function"]["parameters"]}],
               "messages": [{"role": "user", "content": [{"type": "text", "text": "Install axios."}]}]}
    out, frontier = run(env, request=request, api_mode="anthropic_messages")
    assert frontier.calls == 0
    assert out.stop_reason == "tool_use" and out.content[0].type == "tool_use"
    assert out.content[0].input == {"command": "pnpm add axios"}


def test_brain_view_collects_turn_steps():
    req = chat_request("Run the tests then commit.", steps=[("pnpm test", "Tests 3 passed")])
    user, steps, tools = brain_view(req, "chat_completions")
    assert user == "Run the tests then commit."
    assert steps == [{"tool": "terminal", "args": {"command": "pnpm test"}, "result": "Tests 3 passed"}]


def test_strip_habit_block():
    assert strip_habit_block("hi\n\nHabits learned by Hypnos (follow them):\n- a\n- b") == "hi"


def test_text_answer_after_tool_result(env):
    promote(env["conn"], "live")
    env["reply"]["value"] = brain_reply(text="Added axios with pnpm.")
    out, frontier = run(env, request=chat_request("Install axios.", steps=[("pnpm add axios", "+ axios")]))
    assert frontier.calls == 0 and out.choices[0].message.content == "Added axios with pnpm."


def test_passthrough_overhead_under_10ms(env):
    env["conn"].execute("INSERT INTO brains(version, base_model, status, families_json, created_at) VALUES(1,'m','promoted','{}',0)")
    req = chat_request()
    timings = []
    for i in range(300):
        frontier = FakeFrontier()
        t0 = time.perf_counter()
        env["router"].on_llm_execution(request=req, next_call=frontier, api_mode="chat_completions", turn_id=f"t{i}",
                                       session_id="s", platform="cli", api_call_count=2)
        timings.append(time.perf_counter() - t0)
    timings.sort()
    assert timings[int(len(timings) * 0.95)] < 0.010


def test_endpoint_without_logprobs_needs_two_agreeing_samples(env):
    promote(env["conn"], "live")
    replies = iter([brain_reply("pnpm add axios"), brain_reply("npm install axios")])

    def no_logprobs(*_):
        r = next(replies)
        r.pop("hypnos")
        return r

    env["router"]._brain = no_logprobs
    out, frontier = run(env)
    assert frontier.calls == 1 and "disagreed" in routes(env)[-1]["fallback_reason"]
