import json

from hypnos.outcomes import parse_test_output, parse_tool_result


def _result(**kw):
    return json.dumps(kw)


def test_exit_codes():
    assert parse_tool_result("terminal", _result(output="ok", exit_code=0, error=None)).polarity == 1
    assert parse_tool_result("terminal", _result(output="boom", exit_code=2, error=None)).polarity == -1
    assert parse_tool_result("read_file", "file contents").polarity == 0


def test_denial_and_reason():
    res = _result(output="", exit_code=-1, status="blocked",
                  error='BLOCKED: Command denied by user. Reason given by the user: "outside the workdir". Stop.')
    out = parse_tool_result("terminal", res, {"command": "rm -rf ../projects"}, status="error")
    assert out.denied
    assert out.deny_reason == "outside the workdir"
    assert out.polarity == -1


def test_denial_from_error_message_only():
    out = parse_tool_result("terminal", "", status="error", error_message="BLOCKED: User denied this command.")
    assert out.denied


def test_approved_note():
    res = _result(output="", exit_code=0, error=None, approval="Command required approval (x) and was approved by the user.")
    assert parse_tool_result("terminal", res).approved_note


def test_pytest():
    rep = parse_test_output("....F\n===== 4 passed, 1 failed in 0.31s =====")
    assert (rep.framework, rep.passed, rep.failed, rep.ok) == ("pytest", 4, 1, False)
    rep = parse_test_output("===== 12 passed in 1.02s =====")
    assert rep.ok
    rep = parse_test_output("==== 1 passed, 2 errors in 0.5s ====")
    assert rep.failed == 2


def test_jest_and_vitest():
    rep = parse_test_output("Tests:       1 failed, 3 passed, 4 total\nSnapshots: 0 total")
    assert (rep.framework, rep.passed, rep.failed) == ("jest", 3, 1)
    out = " Test Files  2 passed (2)\n      Tests  7 passed (7)\n   Start at  10:00:00"
    rep = parse_test_output(out, "pnpm vitest run")
    assert (rep.framework, rep.passed, rep.failed, rep.ok) == ("vitest", 7, 0, True)


def test_cargo_and_forge():
    rep = parse_test_output("test result: ok. 5 passed; 0 failed; 0 ignored\ntest result: FAILED. 1 passed; 2 failed;")
    assert (rep.framework, rep.passed, rep.failed) == ("cargo", 6, 2)
    rep = parse_test_output("Suite result: ok. 3 passed; 0 failed; 0 skipped; finished in 1.2ms")
    assert (rep.framework, rep.ok) == ("forge", True)


def test_go():
    rep = parse_test_output("--- FAIL: TestRetry (0.00s)\nFAIL\tgithub.com/a/b\t0.01s", "go test ./...")
    assert rep.framework == "go" and rep.failed >= 1
    rep = parse_test_output("ok  \tgithub.com/a/b\t0.012s")
    assert rep.ok


def test_test_report_overrides_exit_code():
    res = _result(output="===== 3 passed in 0.1s =====", exit_code=0, error=None)
    out = parse_tool_result("terminal", res, {"command": "pytest"})
    assert out.tests.ok and out.polarity == 1


def test_garbage_never_raises():
    for junk in [None, 42, "{not json", {"exit_code": "x"}, ["a"]]:
        parse_tool_result("terminal", junk)
