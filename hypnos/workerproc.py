"""Spawn and talk to the training worker, a separate interpreter that has torch installed.

The worker gets a minimal environment: no Hermes or provider credentials ever reach it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

_ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LANGUAGE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "HF_HOME", "HF_HUB_CACHE",
              "HF_HUB_OFFLINE", "HF_HUB_DISABLE_XET", "HF_HUB_ENABLE_HF_TRANSFER", "TRANSFORMERS_CACHE",
              "TRANSFORMERS_OFFLINE", "LD_LIBRARY_PATH", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
              "PYTORCH_CUDA_ALLOC_CONF", "XDG_CACHE_HOME")
_ENV_PREFIX = ("LC_", "CUDA_", "NVIDIA_", "ROCM_", "HIP_", "HYPNOS_FAKE_")

INSTALL_HINT = (
    "The trainer needs its own Python with torch. Hypnos never installs packages itself. Run:\n"
    "  uv venv --python 3.12 {venv}\n"
    "  uv pip install --python {venv}/bin/python -r {root}/pyproject.toml --extra {extra}\n"
    "or point the trainer_python setting at an interpreter that already has torch, transformers and peft.")


def default_trainer_python(data_dir: Path) -> Path:
    return data_dir / "trainer" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def resolve_trainer_python(configured: str, data_dir: Path) -> Path | None:
    for candidate in (configured, str(default_trainer_python(data_dir))):
        if candidate and Path(candidate).expanduser().exists():
            return Path(candidate).expanduser()
    return None


def install_hint(data_dir: Path, extra: str = "") -> str:
    if not extra:
        extra = "cuda" if Path("/proc/driver/nvidia").exists() else ("mlx" if sys.platform == "darwin" else "cpu")
    return INSTALL_HINT.format(venv=data_dir / "trainer", root=PLUGIN_ROOT, extra=extra)


def worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW or k.startswith(_ENV_PREFIX)}
    env["PYTHONPATH"] = str(PLUGIN_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.update(extra or {})
    return env


class WorkerProcess:
    """One worker run. Iterate ``events()``; call ``cancel()`` from another thread to stop it."""

    def __init__(self, python: Path, job_path: Path, log_path: Path, env_extra: dict[str, str] | None = None):
        self.log = open(log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(python), "-m", "hypnos.trainer.worker", str(job_path)],
            cwd=str(PLUGIN_ROOT), env=worker_env(env_extra), stdout=subprocess.PIPE, stderr=self.log,
            stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        self.cancelled = False

    @property
    def pid(self) -> int:
        return self.proc.pid

    def events(self) -> Iterator[dict]:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                self.log.write(f"[stdout] {line}\n")
        self.proc.wait()
        self.log.close()

    def cancel(self, grace: float = 5.0) -> None:
        self.cancelled = True
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            self.proc.terminate()
        deadline = time.time() + grace
        while self.proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                self.proc.kill()

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode


def watch_cancel(should_cancel: Callable[[], bool], worker: WorkerProcess, interval: float = 2.0) -> threading.Thread:
    """Poll a cancel flag (for example the night's DB status) and stop the worker when it flips."""
    def loop():
        while worker.proc.poll() is None:
            try:
                if should_cancel():
                    worker.cancel()
                    return
            except Exception:
                pass
            time.sleep(interval)
    thread = threading.Thread(target=loop, name="hypnos-cancel-watch", daemon=True)
    thread.start()
    return thread
