import pytest

from hypnos import db as dbmod
from hypnos import habits as H
from hypnos.probes import check, segments


def act(command):
    return {"tool": "terminal", "args": {"command": command}}


def first_checker(spec):
    return spec.probes[0].checker


@pytest.mark.parametrize("command,ok", [
    ("pnpm add axios", True),
    ("pnpm install && pnpm run build", True),
    ("npm install axios", False),
    ("npx vitest", False),
    ("yarn add axios", False),
    ("cd web && pnpm add zod", True),
    ("echo done", False),
])
def test_pkg_manager_checker(command, ok):
    spec = H.pkg_manager_habit("pnpm", "pair")
    assert len(spec.probes) == 10
    assert check(first_checker(spec), act(command)).passed is ok


@pytest.mark.parametrize("command,ok", [
    ("rm -rf ./build", True),
    ("rm -rf build dist", True),
    ("rm -rf ~/projects/app/build", False),
    ("rm -rf /tmp/build", False),
    ("rm -rf ../other/build", False),
    ("ls build", False),
    ("trash ./dist", True),
])
def test_safe_delete_checker(command, ok):
    assert check(first_checker(H.safe_delete_habit("pair")), act(command)).passed is ok


@pytest.mark.parametrize("command,ok", [
    ("git checkout -b feat/login && git push -u origin feat/login", True),
    ("git push origin feat/login", True),
    ("git push origin main", False),
    ("git push", False),
    ("git push origin HEAD:main", False),
    ("git switch -c fix/cache && git push", True),
    ("git status", False),
])
def test_branch_checker(command, ok):
    assert check(first_checker(H.branch_habit("pair")), act(command)).passed is ok


@pytest.mark.parametrize("command,ok", [
    ("git commit -am 'feat(auth): add login form'", True),
    ('git add -A && git commit -m "fix: handle empty cache"', True),
    ("git commit -m 'added login form'", False),
    ("git commit -m 'Feat: x'", False),
    ("git status", False),
])
def test_conventional_checker(command, ok):
    assert check(first_checker(H.conventional_commit_habit("pair")), act(command)).passed is ok


@pytest.mark.parametrize("command,ok", [
    ("pnpm test", True),
    ("pnpm test && git commit -m 'feat: x'", True),
    ("pytest -q && git commit -m 'fix: y'", True),
    ("git commit -m 'feat: x' && pnpm test", False),
    ("git commit -m 'feat: x'", False),
])
def test_tests_first_checker(command, ok):
    assert check(first_checker(H.tests_first_habit("pair")), act(command)).passed is ok


def test_checker_rejects_wrong_tool_and_text():
    checker = first_checker(H.pkg_manager_habit("pnpm", "pair"))
    assert check(checker, {"tool": "write_file", "args": {}}).passed is False
    assert check(checker, {"text": "Sure, run pnpm add axios"}).passed is False
    assert check({"type": "judged", "rubric": "x"}, act("ls")).passed is None


def test_segments_respect_quotes():
    assert segments("git commit -m 'a && b' && pnpm test") == ["git commit -m 'a && b'", "pnpm test"]


@pytest.mark.parametrize("rejected,chosen,key", [
    ("npm install lodash", "pnpm add lodash", "pkg_manager:pnpm"),
    ("pip install requests", "uv add requests", "pkg_manager:uv"),
    ("rm -rf ~/projects/app/build", "rm -rf ./build", "safe_delete:workdir"),
    ("git push origin main", "git checkout -b feat/x && git push -u origin feat/x", "git:no_push_main"),
    ("git commit -m 'stuff'", "git commit -m 'feat: add stuff'", "git:conventional_commits"),
    ("git commit -m 'feat: x'", "pnpm test", "git:tests_before_commit"),
    ("ls -la", "eza -la", "prefer:eza:over:ls"),
])
def test_habit_from_pair(rejected, chosen, key):
    spec = H.habit_from_pair(act(rejected), act(chosen), "list the files")
    assert spec is not None and spec.key == key
    assert spec.kind == "procedural"
    # Every deterministic probe accepts the chosen action and rejects the rejected one where they apply.
    if key not in ("git:conventional_commits", "git:tests_before_commit", "prefer:eza:over:ls"):
        assert check(spec.probes[0].checker, act(chosen)).passed
        assert not check(spec.probes[0].checker, act(rejected)).passed


def test_habit_from_unrelated_pair_is_none():
    assert H.habit_from_pair(act("git log"), act("git log --oneline")) is None


@pytest.mark.parametrize("line,key", [
    ("- User prefers pnpm over npm", "pkg_manager:pnpm"),
    ("Always use conventional commits", "git:conventional_commits"),
    ("Never push directly to main; use a feature branch", "git:no_push_main"),
    ("Run the tests before you commit anything", "git:tests_before_commit"),
    ("Selalu pakai uv untuk python", "pkg_manager:uv"),
])
def test_memory_lines_to_habits(line, key):
    spec = H.habit_from_memory_line(line)
    assert spec is not None and spec.key == key


@pytest.mark.parametrize("line", [
    "User's name is Kuda and lives in Bali",
    "The project database is Postgres 16",
    "Email is someone@example.com",
])
def test_declarative_lines_are_skipped(line):
    assert H.habit_from_memory_line(line) is None


def test_unknown_procedural_memory_line_gets_judged_probe():
    spec = H.habit_from_memory_line("Always write docstrings in Google style")
    assert spec is not None and spec.probes[0].kind == "judged"


def test_refresh_end_to_end(tmp_path):
    conn = dbmod.open_db(tmp_path / "d.db")
    conn.execute("INSERT INTO episodes(id, turn_id, session_id, turn_index, started_at, user_message) "
                 "VALUES(1,'t1','s',1,0,'add lodash')")
    conn.execute("INSERT INTO pairs(created_at, source, context_episode, chosen_json, rejected_json, confidence, gold) "
                 "VALUES(0,'correction',1,?,?,0.9,1)",
                 (dbmod.dumps(act("pnpm add lodash")), dbmod.dumps(act("npm install lodash"))))
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "USER.md").write_text("Prefers conventional commits\n§\nName is Kuda\n§\nLives in Bali")
    result = H.refresh(conn, mem)
    assert set(result.created) == {"pkg_manager:pnpm", "git:conventional_commits"}
    assert result.skipped_declarative == 2
    assert conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0] == 20
    assert conn.execute("SELECT habit_id FROM pairs").fetchone()[0] is not None
    again = H.refresh(conn, mem)
    assert again.created == []
    assert conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0] == 2


def test_repeated_pattern_habit(tmp_path):
    conn = dbmod.open_db(tmp_path / "d.db")
    for i in range(6):
        conn.execute("INSERT INTO episodes(id, turn_id, session_id, turn_index, started_at) VALUES(?,?,?,?,0)",
                     (i + 1, f"t{i}", "s", i + 1))
        conn.execute("INSERT INTO steps(episode_id, step_index, kind, tool_name, args_json, outcome, created_at) "
                     "VALUES(?,0,'tool','terminal',?,1,0)", (i + 1, dbmod.dumps({"command": f"pnpm add pkg{i}"})))
    keys = [s.key for s in H.repeated_habits(conn, 5)]
    assert keys == ["pkg_manager:pnpm"]


def test_habit_block():
    assert H.habit_block([]) == ""
    assert H.habit_block(["Use pnpm"]).endswith("- Use pnpm")
