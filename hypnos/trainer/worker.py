"""Training worker. Runs in the trainer interpreter, never inside Hermes.

Usage: python -m hypnos.trainer.worker JOB.json

Reads a job description, streams JSON-line events on stdout, and writes adapters and generations to the
paths the job names. It never touches the network except for downloading the base model the user chose
(from Hugging Face, through the standard cache) and never calls an LLM API.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback


def emit(event: dict) -> None:
    event.setdefault("ts", time.time())
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_jsonl(path: str | None) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_night(job: dict) -> dict:
    from .torch_backend import TorchBackend
    hyper = job.get("hyper") or {}
    emit({"event": "loading", "base_model": job["base_model"], "parent": job.get("parent_adapter")})
    backend = TorchBackend(job["base_model"], job.get("parent_adapter"), trainable=True, lora=hyper.get("lora"),
                           seed=int(job.get("seed", 0)), max_len=int(hyper.get("max_len", 1024)),
                           checkpointing=hyper.get("gradient_checkpointing"))
    emit({"event": "loaded", "device": backend.device, "dtype": str(backend.dtype)})
    if hyper.get("batch_size", "auto") == "auto":
        # Effective batch 8 either way; the orchestrator plans steps with that assumption.
        hyper["batch_size"], hyper["grad_accum"] = (8, 1) if backend.device == "cuda" else (2, 4)
    results = {}
    gen_kwargs = {"max_new_tokens": int(hyper.get("max_new_tokens", 96)), "batch_size": int(hyper.get("gen_batch", 8))}
    for ev in job.get("evals") or []:
        if ev.get("when") == "before":
            prompts = _read_jsonl(ev["prompts_path"])
            emit({"event": "exam_start", "name": ev["name"], "n": len(prompts)})
            outs = backend.generate(prompts, **gen_kwargs)
            _write_jsonl(ev["out_path"], outs)
            results[ev["name"]] = ev["out_path"]
            emit({"event": "exam_end", "name": ev["name"]})
    sft, dpo = _read_jsonl(job.get("sft_path")), _read_jsonl(job.get("dpo_path"))
    stats = backend.train_phases(job.get("phases") or [], sft, dpo, emit=emit,
                                 lr_sft=float(hyper.get("lr_sft", 2e-4)), lr_dpo=float(hyper.get("lr_dpo", 5e-5)),
                                 beta=float(hyper.get("beta", 0.1)), batch_size=int(hyper.get("batch_size", 1)),
                                 grad_accum=int(hyper.get("grad_accum", 4)), deadline=job.get("deadline"))
    backend.save(job["output_adapter"])
    emit({"event": "saved", "adapter": job["output_adapter"]})
    for ev in job.get("evals") or []:
        if ev.get("when", "after") == "after":
            prompts = _read_jsonl(ev["prompts_path"])
            emit({"event": "exam_start", "name": ev["name"], "n": len(prompts)})
            outs = backend.generate(prompts, **gen_kwargs)
            _write_jsonl(ev["out_path"], outs)
            results[ev["name"]] = ev["out_path"]
            emit({"event": "exam_end", "name": ev["name"]})
    summary = {k: v for k, v in stats.items() if not isinstance(v, list)}
    for key in ("sft_loss", "dpo_loss", "dpo_acc"):
        values = stats.get(key) or []
        summary[f"{key}_first"] = values[0] if values else None
        summary[f"{key}_last"] = values[-1] if values else None
    return {"stats": summary, "evals": results}


def run_generate(job: dict) -> dict:
    from .torch_backend import TorchBackend
    hyper = job.get("hyper") or {}
    backend = TorchBackend(job["base_model"], job.get("adapter"), trainable=False)
    prompts = _read_jsonl(job["prompts_path"])
    outs = backend.generate(prompts, max_new_tokens=int(hyper.get("max_new_tokens", 96)),
                            batch_size=int(hyper.get("gen_batch", 8)))
    _write_jsonl(job["out_path"], outs)
    return {"out_path": job["out_path"], "n": len(outs)}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m hypnos.trainer.worker JOB.json", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as fh:
        job = json.load(fh)
    kind = job.get("kind", "night")
    try:
        if kind == "night":
            result = run_night(job)
        elif kind == "generate":
            result = run_generate(job)
        elif kind == "serve":
            from .server import serve
            serve(job, emit)
            result = {}
        elif kind == "probe":
            import torch  # noqa: F401
            result = {"python": sys.version.split()[0], "torch": __import__("torch").__version__,
                      "cuda": __import__("torch").cuda.is_available()}
        else:
            raise ValueError(f"unknown job kind {kind}")
    except Exception as exc:
        emit({"event": "error", "message": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]})
        return 1
    emit({"event": "done", **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
