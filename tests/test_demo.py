import json
import re

from test_night import FAKE

from hypnos import db as dbmod
from hypnos import demo, webapi
from hypnos.dreams import Dreamer, ScriptedTeacher
from hypnos.night import Night, NightConfig


def scripted_brain(endpoint, body, timeout):
    """A well-trained brain: knows the persona's habits."""
    msgs = body["messages"]
    if msgs[-1]["role"] == "tool":
        return {"choices": [{"message": {"role": "assistant", "content": "Done."}}],
                "usage": {"prompt_tokens": 150, "completion_tokens": 3}, "hypnos": {"mean_logprob": -0.02}}
    user = msgs[-1]["content"]
    rules = [(r"Add (\S+) to", lambda m: f"pnpm add {m.group(1)}"),
             (r"Commit the", lambda m: "pnpm test"),
             (r"Tests are green", lambda m: 'git commit -am "feat(app): changes"'),
             (r"Push the (.+) work", lambda m: f"git checkout -b feat/{'-'.join(m.group(1).split())} && "
                                               f"git push -u origin feat/{'-'.join(m.group(1).split())}"),
             (r"Clean up the old (\S+) output", lambda m: f"rm -rf ./{m.group(1)}"),
             (r"Run the tests", lambda m: "pnpm test"), (r"git status", lambda m: "git status -sb"),
             (r"Lint", lambda m: "pnpm lint"), (r"dev server", lambda m: "pnpm dev"),
             (r"last commit", lambda m: "git show --stat HEAD")]
    command = "echo ?"
    for pattern, make in rules:
        m = re.search(pattern, user)
        if m:
            command = make(m)
            break
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "b1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}]}}],
            "usage": {"prompt_tokens": 180, "completion_tokens": 20}, "hypnos": {"mean_logprob": -0.03}}


def test_timelapse_learns_and_hands_over(tmp_path):
    db_path = str(tmp_path / "data.db")
    dbmod.open_db(db_path)
    lines = []

    def night(day):
        cfg = NightConfig(trainer_python=FAKE, cycles=2, max_minutes=5)
        return Night(db_path, tmp_path, cfg, dreamer=Dreamer(ScriptedTeacher(seed=day), k=2, use_sandbox=False)).run()

    summary = demo.Timelapse(db_path=db_path, data_dir=tmp_path, settings={"brain_timeout_s": 5}, days=4,
                             out=lines.append, brain_client=scripted_brain, night_factory=night).run()
    assert summary[0]["corrections"] >= 5 and summary[0]["denials"] >= 2
    assert all(s["corrections"] == 0 for s in summary[1:])
    assert summary[-1]["brain_calls"] > summary[-1]["calls"] // 2
    conn = dbmod.open_db(db_path)
    keys = {r["key"] for r in conn.execute("SELECT key FROM habits")}
    assert keys == {"pkg_manager:pnpm", "git:tests_before_commit", "git:conventional_commits", "git:no_push_main",
                    "safe_delete:workdir"}
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE source != 'synthetic'").fetchone()[0] == 0
    days = webapi.metrics(db_path)["days"]
    assert len(days) == 4 and days[0]["synthetic"]
    assert [d["turns"] for d in days] == [s["turns"] for s in summary]  # each synthetic day on its own date
    assert days[-1]["avg_prompt_tokens"] < days[0]["avg_prompt_tokens"] / 2
    assert days[-1]["success"] >= days[0]["success"]
    assert any("Night 1: promoted" in line for line in lines)
    assert demo.has_real_data(db_path) is False


def test_persona_and_frontier():
    persona = demo.Persona(__import__("random").Random(1))
    task = demo.Task("Add zod to the API.", "pkg_manager:pnpm", "npm install zod", "pnpm add zod", "+ zod")
    assert persona.follows(task, {"tool": "terminal", "args": {"command": "pnpm add zod"}})
    assert not persona.follows(task, {"tool": "terminal", "args": {"command": "npm install zod"}})
    assert persona.correction("pkg_manager:pnpm")
    assert len(demo.SYSTEM) > 10000 and len(demo.TOOLS) == 20
