"""OpenAI-compatible brain server on loopback.

POST /v1/chat/completions   {messages, tools, max_tokens}  -> chat.completion with tool_calls or content,
                            plus ``hypnos: {mean_logprob, brain_version, prompt_tokens}``
GET  /v1/models, GET /health
POST /hypnos/adapter        {path, version}  -> hot-swap the LoRA adapter without a restart

Bound to 127.0.0.1 only. One request is generated at a time.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from ..brainfmt import parse_output


class BrainServer:
    def __init__(self, base_model: str, adapter: str | None, version: int | None):
        from .torch_backend import TorchBackend
        self.base_model = base_model
        self.backend = TorchBackend(base_model, adapter, trainable=False)
        self.adapter = adapter
        self.version = version
        self.lock = threading.Lock()
        self.started = time.time()

    def swap(self, path: str | None, version: int | None) -> None:
        """Load a new adapter. The server keeps the LoRA merged into the weights, so it reloads the base."""
        from .torch_backend import TorchBackend
        fresh = TorchBackend(self.base_model, path, trainable=False)
        with self.lock:
            self.backend = fresh
            self.adapter, self.version = path, version

    def complete(self, body: dict) -> dict:
        example = {"messages": body.get("messages") or [], "tools": body.get("tools") or []}
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 128)
        with self.lock:
            prompt_tokens = self.backend.prompt_tokens(example)
            out = self.backend.generate([example], max_new_tokens=min(max_tokens, 512), batch_size=1)[0]
        action = parse_output(out["text"])
        message: dict = {"role": "assistant", "content": None}
        finish = "stop"
        if action.get("tool"):
            message["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:16]}", "type": "function",
                                      "function": {"name": action["tool"], "arguments": json.dumps(action.get("args") or {})}}]
            finish = "tool_calls"
        else:
            message["content"] = action.get("text") if "text" in action else out["text"]
        return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
                "model": f"hypnos-brain-v{self.version}" if self.version else "hypnos-base",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": out["tokens"],
                          "total_tokens": prompt_tokens + out["tokens"]},
                "hypnos": {"mean_logprob": out["mean_logprob"], "brain_version": self.version,
                           "raw": out["text"][:2000], "valid_parse": "invalid" not in action}}


def make_handler(server: BrainServer, emit: Callable[[dict], None]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.rstrip("/")
            if path.endswith("/health"):
                self._json(200, {"ok": True, "version": server.version, "base_model": server.base_model,
                                 "device": server.backend.device, "uptime_s": round(time.time() - server.started)})
            elif path.endswith("/models"):
                self._json(200, {"object": "list", "data": [{"id": f"hypnos-brain-v{server.version}", "object": "model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.rstrip("/")
                if path.endswith("/chat/completions"):
                    self._json(200, server.complete(body))
                elif path.endswith("/hypnos/adapter"):
                    server.swap(body.get("path"), body.get("version"))
                    emit({"event": "adapter_swapped", "version": body.get("version")})
                    self._json(200, {"ok": True, "version": server.version})
                else:
                    self._json(404, {"error": "not found"})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(job: dict, emit: Callable[[dict], None]) -> None:
    host = "127.0.0.1"
    port = int(job.get("port", 8765))
    server = BrainServer(job["base_model"], job.get("adapter"), job.get("version"))
    httpd = ThreadingHTTPServer((host, port), make_handler(server, emit))
    emit({"event": "serving", "host": host, "port": httpd.server_address[1], "version": server.version,
          "device": server.backend.device})
    httpd.serve_forever()
