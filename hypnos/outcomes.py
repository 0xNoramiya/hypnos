"""Turn a tool result into an outcome: exit code, denial, and test-runner verdicts.

``post_tool_call.result`` for the terminal tool is a JSON string shaped like
``{"output": str, "exit_code": int, "error": str|None, "status"?: "blocked", ...}`` (Hermes v0.23).
Output is stdout and stderr merged, already truncated by Hermes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

DENY_REASON = re.compile(r'Reason given by the user: "(.*?)"\.', re.DOTALL)


@dataclass
class TestReport:
    framework: str
    passed: int
    failed: int

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.passed > 0


@dataclass
class Outcome:
    exit_code: int | None = None
    denied: bool = False
    deny_reason: str = ""
    error: str = ""
    output: str = ""
    approved_note: bool = False
    tests: TestReport | None = None

    @property
    def polarity(self) -> int:
        """+1 success, -1 failure, 0 unknown."""
        if self.denied:
            return -1
        if self.tests is not None:
            return 1 if self.tests.ok else -1
        if self.exit_code is None:
            return -1 if self.error else 0
        return 1 if self.exit_code == 0 else -1


def _int(match: re.Match[str] | None, group: int | str = 1) -> int:
    if not match:
        return 0
    try:
        return int(match.group(group))
    except (TypeError, ValueError, IndexError):
        return 0


_PYTEST = re.compile(r"=+ (?P<body>[^=\n]*?\b(?:passed|failed|error|errors|no tests ran)\b[^=\n]*?) in [\d.]+s",
                     re.IGNORECASE)
_JEST = re.compile(r"^Tests:\s+(?P<body>.+?total)", re.MULTILINE)
_VITEST = re.compile(r"^\s*Tests\s+(?P<body>.*?\d+\s+(?:passed|failed).*?)$", re.MULTILINE)
_CARGO = re.compile(r"test result: (?P<status>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed")
_FORGE = re.compile(r"Suite result: (?P<status>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed")
_GO_OK = re.compile(r"^ok\s+\S+\s+[\d.]+s", re.MULTILINE)
_GO_FAIL = re.compile(r"^(?:FAIL\s+\S+|--- FAIL: )", re.MULTILINE)
_GO_PASS = re.compile(r"^--- PASS: ", re.MULTILINE)


def parse_test_output(output: str, command: str = "") -> TestReport | None:
    """Recognise pytest, jest, vitest, cargo test, forge test and go test summaries."""
    if not output:
        return None
    text = output[-20000:]
    m = _PYTEST.search(text)
    if m:
        body = m.group("body")
        passed = _int(re.search(r"(\d+) passed", body))
        failed = _int(re.search(r"(\d+) failed", body)) + _int(re.search(r"(\d+) errors?", body))
        return TestReport("pytest", passed, failed)
    m = _JEST.search(text)
    if m:
        body = m.group("body")
        return TestReport("jest", _int(re.search(r"(\d+) passed", body)), _int(re.search(r"(\d+) failed", body)))
    m = _VITEST.search(text)
    if m and ("vitest" in text.lower() or "vitest" in command or "Test Files" in text):
        body = m.group("body")
        return TestReport("vitest", _int(re.search(r"(\d+) passed", body)), _int(re.search(r"(\d+) failed", body)))
    forge = list(_FORGE.finditer(text))
    if forge:
        return TestReport("forge", sum(int(x.group("passed")) for x in forge), sum(int(x.group("failed")) for x in forge))
    cargo = list(_CARGO.finditer(text))
    if cargo:
        return TestReport("cargo", sum(int(x.group("passed")) for x in cargo), sum(int(x.group("failed")) for x in cargo))
    if "go test" in command or _GO_OK.search(text) or "--- FAIL:" in text:
        fails = len(_GO_FAIL.findall(text))
        oks = len(_GO_OK.findall(text)) + len(_GO_PASS.findall(text))
        if fails or oks:
            return TestReport("go", oks, fails)
    return None


def parse_tool_result(tool_name: str, result: Any, args: dict | None = None,
                      status: str = "", error_message: str = "") -> Outcome:
    """Normalise a ``post_tool_call`` result. Never raises."""
    outcome = Outcome()
    data: Any = result
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except (ValueError, TypeError):
                data = result
    command = ""
    if isinstance(args, dict):
        command = str(args.get("command") or args.get("cmd") or "")
    if isinstance(data, dict):
        code = data.get("exit_code", data.get("returncode"))
        if isinstance(code, bool):
            code = int(code)
        if isinstance(code, int):
            outcome.exit_code = code
        outcome.output = str(data.get("output") or data.get("stdout") or "")[-20000:]
        error = data.get("error")
        outcome.error = str(error) if error else ""
        if data.get("status") == "blocked" or outcome.error.startswith("BLOCKED"):
            outcome.denied = True
        if data.get("approval"):
            outcome.approved_note = True
    elif isinstance(data, str):
        outcome.output = data[-20000:]
    if not outcome.error and error_message:
        outcome.error = error_message
        if error_message.startswith("BLOCKED"):
            outcome.denied = True
    if status == "error" and not outcome.error and not outcome.denied:
        outcome.error = "tool reported an error"
    m = DENY_REASON.search(outcome.error)
    if m:
        outcome.deny_reason = m.group(1)[:280]
    if tool_name in ("terminal", "shell", "bash", "execute_code", "process") or command:
        outcome.tests = parse_test_output(outcome.output, command)
    return outcome
