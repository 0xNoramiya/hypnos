"""Builders for realistic Hermes hook payloads (v0.23 shapes)."""

import json
import uuid


class Turn:
    """Drives a Recorder or Capture through one user turn, like Hermes would."""

    def __init__(self, sink, session_id, user_message, *, previous_user_message=""):
        self.sink = sink
        self.session_id = session_id
        self.turn_id = f"{session_id}:task:{uuid.uuid4().hex[:8]}"
        self.messages = []
        self.sink("turn_start", {"session_id": session_id, "turn_id": self.turn_id, "user_message": user_message,
                                 "previous_user_message": previous_user_message, "platform": "cli", "model": "frontier"})

    def terminal(self, command, *, exit_code=0, output="", approval=None, deny_reason=None):
        call_id = f"call_{uuid.uuid4().hex[:10]}"
        self.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}]})
        if approval:
            self.sink("approval", {"tool_call_id": call_id, "turn_id": self.turn_id, "choice": approval, "surface": "cli"})
        if approval == "deny":
            error = "BLOCKED: User denied this command. Do NOT retry this command."
            if deny_reason:
                error = f'BLOCKED: Command denied by user. Reason given by the user: "{deny_reason}". Stop.'
            result = json.dumps({"output": "", "exit_code": -1, "error": error, "status": "blocked"})
            status = "error"
        else:
            body = {"output": output, "exit_code": exit_code, "error": None}
            if approval in ("once", "session", "always"):
                body["approval"] = "Command required approval and was approved by the user."
            result = json.dumps(body)
            status = "ok" if exit_code == 0 else "error"
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        self.sink("tool", {"turn_id": self.turn_id, "tool_call_id": call_id, "tool_name": "terminal",
                           "args": {"command": command}, "result": result, "status": status, "duration_ms": 12.0})
        return call_id

    def end(self, text="Done."):
        self.messages.append({"role": "assistant", "content": text})
        self.sink("turn_end", {"turn_id": self.turn_id, "assistant_response": text, "turn_messages": self.messages})
        self.sink("turn_close", {"turn_id": self.turn_id, "completed": True})
