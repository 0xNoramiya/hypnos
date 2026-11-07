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

job = json.load(open(sys.argv[-1]))
policy = os.environ.get("HYPNOS_FAKE_POLICY", "pnpm")


def emit(e):
    e["ts"] = time.time()
    print(json.dumps(e), flush=True)


def answer(item, trained):
    if policy == "garbage" and trained:
        return '<tool_call>{"name": "terminal", "arguments": {"command": </tool_call>'
    if item["messages"][-1]["role"] == "tool":
        return "Done."
    user = [m for m in item["messages"] if m["role"] == "user"][-1]["content"]
    words = re.findall(r"[a-z][a-z0-9-]+", user.lower())
    pkg = words[-1] if words else "x"
    if not trained:
        return f'<tool_call>{{"name": "terminal", "arguments": {{"command": "npm install {pkg}"}}}}</tool_call>'
    return f'<tool_call>{{"name": "terminal", "arguments": {{"command": "{policy} add {pkg}"}}}}</tool_call>'


def run_evals(when):
    for ev in job.get("evals", []):
        if ev.get("when", "after") != when:
            continue
        items = [json.loads(line) for line in open(ev["prompts_path"]) if line.strip()]
        emit({"event": "exam_start", "name": ev["name"], "n": len(items)})
        with open(ev["out_path"], "w") as fh:
            for it in items:
                fh.write(json.dumps({"id": it["id"], "text": answer(it, when == "after"), "mean_logprob": -0.1,
                                     "tokens": 20}) + "\n")
        emit({"event": "exam_end", "name": ev["name"]})


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
open(os.path.join(job["output_adapter"], "adapter_config.json"), "w").write("{}")
emit({"event": "saved", "adapter": job["output_adapter"]})
run_evals("after")
emit({"event": "done", "stats": {"sft_steps": 1}, "evals": {}})
