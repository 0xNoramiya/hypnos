"""A night of sleep: the orchestrator.

Runs inside a Hermes process (``hermes hypnos sleep``) so teacher calls can use ``ctx.llm``. All torch work
happens in the worker subprocess. Every stage writes events to ``night_events``; the hypnogram is drawn
from them.

Stages, as in human sleep: WAKE -> N1 (scrub check) -> N2 (sort the day: families, splits, habits) ->
cycles of N3 (slow-wave replay: SFT) and REM (dreams: DPO), N3-heavy early and REM-heavy late ->
WAKE (the waking exam, promotion or rollback, the morning report).
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import datasets as ds
from . import db as dbmod
from . import exam as exammod
from . import habits as habitsmod
from .brainfmt import brain_tools, build_messages
from .scrub import contains_secret, scrub_text
from .workerproc import WorkerProcess, install_hint, resolve_trainer_python, watch_cancel

STAGES = ("WAKE", "REM", "N1", "N2", "N3")


class NightError(RuntimeError):
    pass


@dataclass
class NightConfig:
    base_model: str = "Qwen/Qwen3-0.6B"
    tiny_model: str = "HuggingFaceTB/SmolLM2-135M-Instruct"
    tiny: bool = False
    cycles: int = 4
    max_minutes: float = 60.0
    epochs_sft: float = 3.0
    epochs_dpo: float = 3.0
    min_steps: int = 8
    max_steps: int = 400
    replay_ratio: float = 0.5
    max_heldout: int = 60
    probes_per_habit: int = 10
    trainer_python: str = ""
    consolidation_threshold: float = 0.9
    habit_text_share: float = 0.5
    hyper: dict = field(default_factory=dict)
    trigger: str = "manual"

    @property
    def model(self) -> str:
        return self.tiny_model if self.tiny else self.base_model


def distribute(total: int, weights: list[int]) -> list[int]:
    if total <= 0 or not weights:
        return [0] * len(weights)
    s = sum(weights)
    raw = [total * w / s for w in weights]
    out = [int(math.floor(x)) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[: total - sum(out)]:
        out[i] += 1
    return out


def schedule(cycles: int, sft_steps: int, dpo_steps: int) -> list[dict]:
    """Early cycles are N3-heavy, later cycles REM-heavy, like human sleep architecture."""
    cycles = max(1, cycles)
    n3 = distribute(sft_steps, [cycles - c for c in range(cycles)])
    rem = distribute(dpo_steps, [c + 1 for c in range(cycles)])
    phases = []
    for c in range(cycles):
        phases.append({"stage": "N3", "cycle": c + 1, "kind": "sft", "steps": n3[c]})
        phases.append({"stage": "REM", "cycle": c + 1, "kind": "dpo", "steps": rem[c]})
    return phases


def steps_for(n_examples: int, epochs: float, cfg: NightConfig, effective_batch: int = 8) -> int:
    if n_examples <= 0:
        return 0
    steps = int(math.ceil(epochs * n_examples / effective_batch))
    return max(cfg.min_steps, min(cfg.max_steps, steps))


class Night:
    def __init__(self, db_path: str, data_dir: Path, cfg: NightConfig, *, memories_dir: Path | None = None,
                 broadcast: Callable[[str, dict], None] | None = None, log: Callable[[str], None] = lambda s: None,
                 dreamer: Any = None, lethe: Any = None):
        self.lethe = lethe
        self.db_path = db_path
        self.data_dir = Path(data_dir)
        self.cfg = cfg
        self.memories_dir = memories_dir
        self.broadcast = broadcast or (lambda name, payload: None)
        self.log = log
        self.dreamer = dreamer
        self.conn = dbmod.open_db(db_path)
        self.night_id: int | None = None
        self.started = time.time()
        self._lock_fh = None

    # --- bookkeeping ---------------------------------------------------------------------------------

    def event(self, stage: str, event: str, cycle: int | None = None, **detail: Any) -> None:
        self.conn.execute("INSERT INTO night_events(night_id, ts, stage, cycle, event, detail_json) VALUES(?,?,?,?,?,?)",
                          (self.night_id, time.time(), stage, cycle, event, dbmod.dumps(detail)))
        payload = {"night_id": self.night_id, "stage": stage, "cycle": cycle, "event": event, **detail}
        self.broadcast("night.stage", payload)
        self.log(f"[{stage:<4}] {event}" + (f" (cycle {cycle})" if cycle else "") +
                 (f" {json.dumps(detail, default=str)[:160]}" if detail else ""))

    def _lock(self) -> None:
        path = self.data_dir / "night.lock"
        self._lock_fh = open(path, "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise NightError("Hypnos is already asleep (another night is running).") from exc
        self._lock_fh.write(str(os.getpid()))
        self._lock_fh.flush()

    def _unlock(self) -> None:
        if self._lock_fh:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            finally:
                self._lock_fh.close()
                self._lock_fh = None

    def _cancelled(self) -> bool:
        row = self.conn.execute("SELECT status FROM nights WHERE id = ?", (self.night_id,)).fetchone()
        return bool(row and row["status"] == "cancelling")

    def _finish(self, status: str, report: dict | None = None, brain_version: int | None = None) -> None:
        self.conn.execute("UPDATE nights SET status = ?, ended_at = ?, report_json = ?, brain_version = ? WHERE id = ?",
                          (status, time.time(), dbmod.dumps(report or {}), brain_version, self.night_id))

    # --- stages --------------------------------------------------------------------------------------

    def verify_scrub(self, since: float) -> int:
        """N1: re-check everything captured since the last night and re-scrub any survivor."""
        fixed = 0
        checks = [("episodes", "id", ("user_message", "assistant_text"), "started_at"),
                  ("steps", "id", ("args_json", "text", "result_text"), "created_at"),
                  ("pairs", "id", ("chosen_json", "rejected_json"), "created_at")]
        for table, key, cols, ts_col in checks:
            rows = self.conn.execute(f"SELECT {key}, {', '.join(cols)} FROM {table} WHERE {ts_col} >= ?", (since,)).fetchall()
            for row in rows:
                for col in cols:
                    value = row[col]
                    if value and contains_secret(value):
                        self.conn.execute(f"UPDATE {table} SET {col} = ? WHERE {key} = ?", (scrub_text(value), row[key]))
                        fixed += 1
        return fixed

    def _parent(self):
        return self.conn.execute("SELECT * FROM brains WHERE status = 'promoted' ORDER BY version DESC LIMIT 1").fetchone()

    def _seen_episodes(self) -> set[int]:
        seen: set[int] = set()
        for row in self.conn.execute("SELECT manifest_json FROM brains WHERE status IN ('promoted', 'retired')"):
            manifest = dbmod.loads(row["manifest_json"], {})
            seen.update(manifest.get("episodes", []))
        return seen

    def _habit_rows(self):
        return self.conn.execute("SELECT * FROM habits WHERE status = 'active' AND excluded = 0 ORDER BY id").fetchall()

    def build_data(self, night_dir: Path, parent) -> dict:
        """N2: choose what to replay and how to examine, and write the worker's files."""
        rng = random.Random(self.night_id or 0)
        habit_rows = self._habit_rows()
        habit_texts = [h["statement"] for h in habit_rows]
        if self.lethe is not None:
            # Retraining after Lethe: exactly the contaminated brains' data, minus what was forgotten.
            new = ds.episode_examples(self.conn, "train", episode_ids=self.lethe.retrain_episodes)
            replay = []
            pairs = ds.pair_examples(self.conn, pair_ids=self.lethe.retrain_pairs)
        else:
            seen = self._seen_episodes()
            train_all = ds.episode_examples(self.conn, "train")
            new = [e for e in train_all if e.episode_id not in seen]
            old = [e for e in train_all if e.episode_id in seen]
            replay = rng.sample(old, min(len(old), int(len(new) * self.cfg.replay_ratio) + 4)) if old else []
            pairs = ds.pair_examples(self.conn)
        dream_ids = [r[0] for r in self.conn.execute("SELECT id FROM dreams WHERE night_id = ? AND excluded = 0",
                                                     (self.night_id,))]
        sft_rows, seen_keys = [], set()
        for ex in new + replay + [p.as_sft() for p in pairs]:
            key = json.dumps([ex.user, ex.history, ex.target], sort_keys=True, default=str)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            with_text = ds.habit_text_mask(ex.id, self.cfg.habit_text_share)
            sft_rows.append(ex.render(habit_texts if with_text else []))
        dpo_rows = [p.render([]) for p in pairs]
        exam_items = []
        heldout = ds.episode_examples(self.conn, "heldout")[-self.cfg.max_heldout:]
        for ex in heldout:
            row = ex.render(habit_texts)
            row["kind"] = "heldout"
            exam_items.append(row)
        habit_families = {}
        for h in habit_rows:
            habit_families[h["id"]] = h["family"]
            probes = self.conn.execute("SELECT * FROM probes WHERE habit_id = ? AND kind = 'deterministic' ORDER BY id",
                                       (h["id"],)).fetchall()[: self.cfg.probes_per_habit]
            for p in probes:
                prompt = dbmod.loads(p["prompt_json"], {})
                exam_items.append({"id": f"probe:{p['id']}", "kind": "probe", "habit_id": h["id"], "family": h["family"],
                                   "messages": build_messages(prompt.get("user", ""), prompt.get("history"), []),
                                   "tools": brain_tools(None, h["family"] or "shell"),
                                   "checker": dbmod.loads(p["checker_json"], {})})
        if self.dreamer:
            exam_items.extend(self.dreamer.exam_items())
        elif self.lethe is not None:
            from .dreams import dream_exam_items
            exam_items.extend(dream_exam_items(self.conn, self.night_id))
        paths = {name: night_dir / f"{name}.jsonl" for name in ("sft", "dpo", "exam")}
        ds.write_jsonl(paths["sft"], sft_rows)
        ds.write_jsonl(paths["dpo"], dpo_rows)
        ds.write_jsonl(paths["exam"], [{k: v for k, v in item.items() if k in ("id", "messages", "tools")} for item in exam_items])
        manifest = ds.Manifest(self.cfg.model, parent["version"] if parent else None,
                               episodes=[e.episode_id for e in new if e.episode_id],
                               pairs=[int(p.id.split(":")[1]) for p in pairs],
                               habits=[h["id"] for h in habit_rows],
                               replay=[e.episode_id for e in replay if e.episode_id],
                               params={"cycles": self.cfg.cycles, "epochs_sft": self.cfg.epochs_sft,
                                       "epochs_dpo": self.cfg.epochs_dpo, "habit_text_share": self.cfg.habit_text_share,
                                       "dreams": dream_ids})
        return {"paths": paths, "sft": sft_rows, "dpo": dpo_rows, "exam_items": exam_items, "manifest": manifest,
                "habit_families": habit_families, "new": len(new), "replay": len(replay), "pairs": len(pairs),
                "dreams": len(dream_ids), "heldout": len(heldout)}

    # --- the night -----------------------------------------------------------------------------------

    def run(self) -> dict:
        self._lock()
        try:
            return self._run()
        finally:
            self._unlock()

    def _run(self) -> dict:
        cfg = self.cfg
        last = self.conn.execute("SELECT MAX(ended_at) FROM nights WHERE status IN ('done', 'bad_night')").fetchone()[0] or 0
        stale = self.conn.execute("SELECT id FROM nights WHERE status IN ('running', 'cancelling')").fetchall()
        for row in stale:
            self.conn.execute("UPDATE nights SET status = 'interrupted', ended_at = ? WHERE id = ?", (time.time(), row["id"]))
        cur = self.conn.execute("INSERT INTO nights(started_at, status, trigger, params_json, pid) VALUES(?,?,?,?,?)",
                                (self.started, "running", cfg.trigger, dbmod.dumps(cfg.__dict__), os.getpid()))
        self.night_id = cur.lastrowid
        night_dir = self.data_dir / "nights" / str(self.night_id)
        night_dir.mkdir(parents=True, exist_ok=True)
        deadline = self.started + cfg.max_minutes * 60
        try:
            self.event("WAKE", "lights_out", trigger=cfg.trigger, model=cfg.model)
            self.event("N1", "scrub_check")
            fixed = self.verify_scrub(last)
            self.event("N1", "scrub_verified", rescrubbed=fixed)
            self.event("N2", "sorting_the_day")
            ds.assign_families(self.conn)
            splits = ds.assign_splits(self.conn)
            refreshed = habitsmod.refresh(self.conn, self.memories_dir)
            python = resolve_trainer_python(cfg.trainer_python, self.data_dir)
            parent = self._parent()
            if self.lethe is not None:
                parent = (self.conn.execute("SELECT * FROM brains WHERE version = ?", (self.lethe.parent,)).fetchone()
                          if self.lethe.parent else None)
                self.event("N2", "lethe", forgetting=f"{self.lethe.kind} {self.lethe.target}",
                           contaminated=self.lethe.affected, parent=self.lethe.parent)
            if parent and parent["base_model"] != cfg.model:
                self.event("N2", "new_base_model", was=parent["base_model"], now=cfg.model)
                parent = None
            if self.dreamer:
                if python is not None and getattr(self.dreamer, "_generate", None) is None:
                    from .dreams import worker_generate
                    self.dreamer._generate = worker_generate(python, cfg.model, parent["adapter_path"] if parent else None,
                                                             {"batch_size": "auto", **cfg.hyper})
                self.event("N2", "dreaming_up_variants")
                self.dreamer.prepare(self)
            data = self.build_data(night_dir, parent)
            self.event("N2", "plan", new_episodes=data["new"], replay=data["replay"], pairs=data["pairs"],
                       dreams=data["dreams"], heldout=data["heldout"], exam_items=len(data["exam_items"]),
                       habits_new=len(refreshed.created), splits=splits)
            if not data["sft"] and not data["dpo"]:
                self.event("WAKE", "nothing_to_learn")
                report = {"status": "skipped", "reason": "Nothing new to learn. Use Hermes for a while first."}
                self._finish("skipped", report)
                return report
            if python is None:
                raise NightError(install_hint(self.data_dir))
            phases = schedule(cfg.cycles, steps_for(len(data["sft"]), cfg.epochs_sft, cfg),
                              steps_for(len(data["dpo"]), cfg.epochs_dpo, cfg))
            version = (self.conn.execute("SELECT MAX(version) FROM brains").fetchone()[0] or 0) + 1
            adapter_dir = self.data_dir / "brains" / f"v{version}"
            job = {"kind": "night", "base_model": cfg.model, "seed": self.night_id,
                   "parent_adapter": parent["adapter_path"] if parent else None,
                   "output_adapter": str(adapter_dir), "sft_path": str(data["paths"]["sft"]),
                   "dpo_path": str(data["paths"]["dpo"]), "phases": phases, "deadline": deadline - 120,
                   "hyper": {"batch_size": "auto", **cfg.hyper},
                   "evals": [{"name": "baseline", "when": "before", "prompts_path": str(data["paths"]["exam"]),
                              "out_path": str(night_dir / "baseline_out.jsonl")},
                             {"name": "candidate", "when": "after", "prompts_path": str(data["paths"]["exam"]),
                              "out_path": str(night_dir / "candidate_out.jsonl")}]}
            job_path = night_dir / "job.json"
            job_path.write_text(json.dumps(job, indent=1))
            result = self._run_worker(python, job_path, night_dir)
            self.event("WAKE", "waking_exam", items=len(data["exam_items"]))
            baseline_out = ds.read_jsonl(night_dir / "baseline_out.jsonl")
            candidate_out = ds.read_jsonl(night_dir / "candidate_out.jsonl")
            base_scores, _ = exammod.grade(data["exam_items"], baseline_out)
            cand_scores, results = exammod.grade(data["exam_items"], candidate_out)
            decision = exammod.decide(cand_scores, base_scores, threshold=cfg.consolidation_threshold,
                                      habit_families=data["habit_families"])
            if self.dreamer:
                self.dreamer.after_exam(results)
            brain_status = "promoted" if decision.promote else "rejected"
            exam_record = {"candidate": cand_scores.as_dict(), "baseline": base_scores.as_dict(),
                           "baseline_label": f"v{parent['version']}" if parent else "base model",
                           "decision": {"promote": decision.promote, "reasons": decision.reasons,
                                        "families": decision.families, "consolidated": decision.consolidated_habits},
                           "train": result.get("stats", {})}
            manifest = data["manifest"]
            families = {f: {"state": "shadow", "agree": 0, "turns": 0} for f in decision.families}
            if parent and decision.promote:
                for f, state in dbmod.loads(parent["families_json"], {}).items():
                    if f in families and state.get("state") == "live":
                        families[f] = state
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO brains(version, parent_version, base_model, backend, adapter_path, manifest_hash, "
                    "manifest_json, exam_json, families_json, status, night_id, created_at, promoted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (version, parent["version"] if parent else None, cfg.model, "torch", str(adapter_dir),
                     manifest.digest(), dbmod.dumps(manifest.as_dict()), dbmod.dumps(exam_record),
                     dbmod.dumps(families), brain_status, self.night_id, time.time(),
                     time.time() if decision.promote else None))
                if decision.promote and parent:
                    self.conn.execute("UPDATE brains SET status = 'retired' WHERE version = ?", (parent["version"],))
                for habit_id, (p, t) in cand_scores.habits.items():
                    self.conn.execute(
                        "INSERT OR REPLACE INTO habit_results(habit_id, brain_version, with_text, passes, total, consolidated, "
                        "created_at) VALUES(?,?,0,?,?,?,?)",
                        (habit_id, version, p, t, int(habit_id in decision.consolidated_habits), time.time()))
                for habit_id, (p, t) in base_scores.habits.items():
                    self.conn.execute(
                        "INSERT OR REPLACE INTO habit_results(habit_id, brain_version, with_text, passes, total, consolidated, "
                        "created_at) VALUES(?,?,0,?,?,0,?)",
                        (habit_id, (parent["version"] if parent else 0), p, t, time.time()))
                self.conn.execute("UPDATE signals SET consolidated_night = ? WHERE consolidated_night IS NULL AND created_at <= ?",
                                  (self.night_id, self.started))
                self.conn.execute("UPDATE pairs SET night_id = ? WHERE night_id IS NULL AND created_at <= ?",
                                  (self.night_id, self.started))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            if self.lethe is not None:
                from .lineage import retire_contaminated
                gone = retire_contaminated(self.conn, self.lethe, version if decision.promote else None)
                self.event("WAKE", "lethe_done", retired=gone, new_version=version, promoted=decision.promote)
            from .report import build_report
            report = build_report(self.conn, self.night_id, version)
            status = "done" if decision.promote else "bad_night"
            self._finish(status, report, version)
            self.event("WAKE", "woke_up", promoted=decision.promote, version=version, families=decision.families)
            self.broadcast("night.done", {"night_id": self.night_id, "version": version, "promoted": decision.promote})
            return report
        except NightError as exc:
            self.event("WAKE", "woke_early", reason=str(exc).splitlines()[0])
            self._finish("failed", {"status": "failed", "reason": str(exc)})
            raise
        except Exception as exc:
            status = "cancelled" if self._cancelled() else "failed"
            self.event("WAKE", "woke_early", reason=f"{type(exc).__name__}: {exc}"[:300])
            self._finish(status, {"status": status, "reason": f"{type(exc).__name__}: {exc}"})
            raise

    def _run_worker(self, python: Path, job_path: Path, night_dir: Path) -> dict:
        worker = WorkerProcess(python, job_path, night_dir / "worker.log")
        self.conn.execute("UPDATE nights SET checkpoint_json = ? WHERE id = ?",
                          (dbmod.dumps({"worker_pid": worker.pid}), self.night_id))
        watch_cancel(self._cancelled, worker)
        result: dict = {}
        error = ""
        stage = "N2"
        last_progress = 0.0
        for ev in worker.events():
            kind = ev.get("event")
            if kind == "loading":
                self.event("N2", "loading_brain", parent=ev.get("parent"))
            elif kind == "loaded":
                self.event("N2", "brain_loaded", device=ev.get("device"))
            elif kind == "exam_start" and ev.get("name") == "baseline":
                self.event("N2", "pre_sleep_test", items=ev.get("n"))
            elif kind == "exam_start":
                self.event("WAKE", "waking_up", items=ev.get("n"))
            elif kind == "phase_start":
                if stage in ("N3", "REM") and ev.get("stage") == "N3":
                    self.event("N2", "cycle_transition", cycle=ev.get("cycle"))
                stage = ev.get("stage", stage)
                self.event(stage, "phase_start", cycle=ev.get("cycle"), steps=ev.get("steps"), kind=ev.get("kind"))
            elif kind == "phase_skipped":
                pass
            elif kind == "progress":
                if time.time() - last_progress > 1.0 or ev.get("step") == ev.get("steps"):
                    last_progress = time.time()
                    self.event(ev.get("stage", stage), "progress", cycle=ev.get("cycle"), step=ev.get("step"),
                               steps=ev.get("steps"), loss=ev.get("loss"))
            elif kind == "phase_end":
                self.event(ev.get("stage", stage), "phase_end", cycle=ev.get("cycle"), mean_loss=ev.get("mean_loss"))
            elif kind == "saved":
                self.event(stage, "brain_saved")
            elif kind == "error":
                error = ev.get("message", "worker error")
            elif kind == "done":
                result = ev
        if worker.cancelled:
            raise NightError("Sleep was cancelled.")
        if worker.returncode != 0 or error:
            raise NightError(f"The training worker failed: {error or f'exit code {worker.returncode}'}. "
                             f"See {night_dir / 'worker.log'}")
        return result


def request_cancel(conn) -> bool:
    cur = conn.execute("UPDATE nights SET status = 'cancelling' WHERE status = 'running'")
    return cur.rowcount > 0
