"""A scripted OpenAI-compatible server that stands in for the frontier model in end-to-end tests.

Usage: python fake_frontier.py SCRIPT.json PORT LOG.jsonl

SCRIPT.json is a list of replies consumed in order. Each reply is either
``{"tool": "terminal", "args": {"command": "..."}}`` or ``{"text": "..."}``. When the script runs out,
the server answers "ok". Every request body is appended to LOG.jsonl.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

LOCK = threading.Lock()


class State:
    script: ClassVar[list] = []
    log_path: str = ""


def _reply_message(reply: dict) -> dict:
    if "tool" in reply:
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
            "function": {"name": reply["tool"], "arguments": json.dumps(reply.get("args") or {})}}]}
    return {"role": "assistant", "content": reply.get("text", "ok")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "fake-frontier", "object": "model", "owned_by": "test"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        agent_call = bool(body.get("tools"))
        with LOCK:
            if State.log_path:
                with open(State.log_path, "a") as fh:
                    fh.write(json.dumps({"path": self.path, "agent_call": agent_call, **body}) + "\n")
            if agent_call:
                reply = State.script.pop(0) if State.script else {"text": "ok"}
            else:
                reply = {"text": "Scripted session"}
        message = _reply_message(reply)
        prompt_tokens = sum(len(json.dumps(m)) for m in body.get("messages", [])) // 4
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 12, "total_tokens": prompt_tokens + 12}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        cid = f"chatcmpl-{uuid.uuid4().hex[:10]}"
        if not body.get("stream"):
            self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()),
                             "model": body.get("model", "fake-frontier"),
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(delta: dict, finish_reason=None, with_usage=False):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": body.get("model", "fake-frontier"),
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            if with_usage:
                payload["usage"] = usage
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())

        chunk({"role": "assistant"})
        if message.get("tool_calls"):
            call = message["tool_calls"][0]
            chunk({"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                   "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]}}]})
        else:
            chunk({"content": message["content"]})
        chunk({}, finish)
        chunk({}, None, with_usage=True) if body.get("stream_options", {}).get("include_usage") else None
        self.wfile.write(b"data: [DONE]\n\n")


def main() -> None:
    script_path, port, log_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    with open(script_path) as fh:
        State.script = json.load(fh)
    State.log_path = log_path
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"fake frontier on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
