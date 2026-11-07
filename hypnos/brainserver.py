"""Start, stop and talk to the local brain server (a background worker process on 127.0.0.1).

Disclosed background process: it runs only after a brain has been promoted and routing is on, or when
the user runs ``hermes hypnos serve start``. Users can point ``brain_endpoint`` at their own
OpenAI-compatible server instead, and then Hypnos starts nothing.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .workerproc import PLUGIN_ROOT, resolve_trainer_python, worker_env

DEFAULT_PORT = 8765


def _state_path(data_dir: Path) -> Path:
    return data_dir / "brain-server.json"


def read_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def http_json(url: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback or user-configured endpoint)
        return json.loads(resp.read() or b"{}")


def endpoint(data_dir: Path, configured: str = "") -> str:
    if configured:
        return configured.rstrip("/")
    state = read_state(data_dir)
    return f"http://127.0.0.1:{state.get('port', DEFAULT_PORT)}/v1"


def health(base_url: str, timeout: float = 2.0) -> dict | None:
    root = base_url[:-3] if base_url.endswith("/v1") else base_url
    try:
        return http_json(f"{root}/health", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def status(data_dir: Path) -> dict:
    state = read_state(data_dir)
    running = _alive(state.get("pid"))
    info = {"running": running, **state}
    if running:
        info["health"] = health(f"http://127.0.0.1:{state.get('port', DEFAULT_PORT)}/v1")
    return info


def start(data_dir: Path, *, base_model: str, adapter: str | None, version: int | None, trainer_python: str = "",
          port: int = DEFAULT_PORT, wait: float = 180.0) -> dict:
    """Start the server if it is not running; hot-swap the adapter if it is. Returns the state."""
    current = status(data_dir)
    if current.get("running"):
        if current.get("base_model") == base_model:
            if current.get("version") != version:
                swap(data_dir, adapter, version)
            return status(data_dir)
        stop(data_dir)
    python = resolve_trainer_python(trainer_python, data_dir)
    if python is None:
        raise RuntimeError("No trainer interpreter found. Run `hermes hypnos doctor` for the install command.")
    job_path = data_dir / "brain-server-job.json"
    job_path.write_text(json.dumps({"kind": "serve", "base_model": base_model, "adapter": adapter,
                                    "version": version, "port": port}))
    log = open(data_dir / "brain-server.log", "a", encoding="utf-8")
    proc = subprocess.Popen([str(python), "-m", "hypnos.trainer.worker", str(job_path)], cwd=str(PLUGIN_ROOT),
                            env=worker_env(), stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    state = {"pid": proc.pid, "port": port, "base_model": base_model, "version": version, "adapter": adapter,
             "started_at": time.time()}
    _state_path(data_dir).write_text(json.dumps(state))
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"The brain server exited early. See {data_dir / 'brain-server.log'}")
        if health(f"http://127.0.0.1:{port}/v1"):
            return status(data_dir)
        time.sleep(1.0)
    return status(data_dir)


def swap(data_dir: Path, adapter: str | None, version: int | None) -> None:
    state = read_state(data_dir)
    http_json(f"http://127.0.0.1:{state.get('port', DEFAULT_PORT)}/hypnos/adapter",
              {"path": adapter, "version": version}, timeout=120)
    state.update({"adapter": adapter, "version": version})
    _state_path(data_dir).write_text(json.dumps(state))


def stop(data_dir: Path) -> bool:
    state = read_state(data_dir)
    pid = state.get("pid")
    if not _alive(pid):
        _state_path(data_dir).unlink(missing_ok=True)
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    _state_path(data_dir).unlink(missing_ok=True)
    return True
