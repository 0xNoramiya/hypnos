#!/usr/bin/env python3
"""Stands in for the trainer interpreter in tests: speaks the worker protocol without torch.

Invoked as ``fake_trainer.py -m hypnos.trainer.worker JOB.json``. The candidate brain follows the policy in
HYPNOS_FAKE_POLICY (default: pnpm); the baseline answers like an untrained model (npm).
"""
import json
import os
import re
import sys
import time
from pathlib import Path

job = json.loads(Path(sys.argv[-1]).read_text())
policy = os.environ.get("HYPNOS_FAKE_POLICY", "pnpm")


def emit(e):
    e["ts"] = time.time()
    print(json.dumps(e), flush=True)


GOOD_RULES = [
    (r"Add (\S+) to", lambda m: f"pnpm add {m.group(1)}"),
    (r"Commit the", lambda m: "pnpm test"),
    (r"Tests are green", lambda m: 'git commit -am "feat(app): changes"'),
    (r"Push the (.+) work", lambda m: "git checkout -b feat/{0} && git push -u origin feat/{0}".format("-".join(m.group(1).split()))),
    (r"Clean up the old (\S+) output", lambda m: f"rm -rf ./{m.group(1)}"),
    (r"Run the tests", lambda m: "pnpm test"), (r"git status", lambda m: "git status -sb"),
    (r"Lint", lambda m: "pnpm lint"), (r"dev server", lambda m: "pnpm dev"),
    (r"last commit", lambda m: "git show --stat HEAD"),
]


def good_student(user):
    if policy != "pnpm":
        return None
    for pattern, make in GOOD_RULES:
        m = re.search(pattern, user)
        if m:
            return f'<tool_call>{{"name": "terminal", "arguments": {{"command": {json.dumps(make(m))}}}}}</tool_call>'
    return None


def answer(item, trained):
    if policy == "garbage" and trained:
        return '<tool_call>{"name": "terminal", "arguments": {"command": </tool_call>'
    if item["messages"][-1]["role"] == "tool":
        return "Done."
    user = [m for m in item["messages"] if m["role"] == "user"][-1]["content"]
    named = re.search(r"\badd\s+([a-z0-9@/._-]+)", user, re.IGNORECASE)
    words = re.findall(r"[a-z][a-z0-9-]+", user.lower())
    pkg = named.group(1).lower() if named else (words[-1] if words else "x")
    if not trained:
        return f'<tool_call>{{"name": "terminal", "arguments": {{"command": "npm install {pkg}"}}}}</tool_call>'
    good = good_student(user)
    if good:
        return good
    return f'<tool_call>{{"name": "terminal", "arguments": {{"command": "{policy} add {pkg}"}}}}</tool_call>'


def run_evals(when):
    for ev in job.get("evals", []):
        if ev.get("when", "after") != when:
            continue
        items = [json.loads(line) for line in Path(ev["prompts_path"]).read_text().splitlines() if line.strip()]
        emit({"event": "exam_start", "name": ev["name"], "n": len(items)})
        with open(ev["out_path"], "w") as fh:
            fh.writelines(json.dumps({"id": it["id"], "text": answer(it, when == "after"), "mean_logprob": -0.1,
                                     "tokens": 20}) + "\n" for it in items)
        emit({"event": "exam_end", "name": ev["name"]})


if job.get("kind") == "generate":
    items = [json.loads(line) for line in Path(job["prompts_path"]).read_text().splitlines() if line.strip()]
    with open(job["out_path"], "w") as fh:
        fh.writelines(json.dumps({"id": it["id"], "text": answer(it, bool(job.get("adapter"))), "mean_logprob": -0.5,
                                 "tokens": 20}) + "\n" for it in items)
    emit({"event": "done", "out_path": job["out_path"], "n": len(items)})
    sys.exit(0)
if os.environ.get("HYPNOS_FAKE_FAIL"):
    emit({"event": "error", "message": "simulated failure"})
    sys.exit(1)
emit({"event": "loading", "base_model": job["base_model"], "parent": job.get("parent_adapter")})
emit({"event": "loaded", "device": "cpu", "dtype": "float32"})
run_evals("before")
for phase in job.get("phases", []):
    if phase["steps"] <= 0:
        emit({"event": "phase_skipped", **phase})
        continue
    emit({"event": "phase_start", **phase})
    for s in range(phase["steps"]):
        emit({"event": "progress", "stage": phase["stage"], "cycle": phase["cycle"], "kind": phase["kind"],
              "step": s + 1, "steps": phase["steps"], "loss": round(1.0 / (s + 1), 4)})
    emit({"event": "phase_end", **phase, "mean_loss": 0.3})
os.makedirs(job["output_adapter"], exist_ok=True)
Path(job["output_adapter"], "adapter_config.json").write_text("{}")
emit({"event": "saved", "adapter": job["output_adapter"]})
run_evals("after")
emit({"event": "done", "stats": {"sft_steps": 1}, "evals": {}})
