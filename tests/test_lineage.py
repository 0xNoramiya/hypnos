import json
from pathlib import Path

import pytest
from test_night import FAKE, populate

from hypnos import db as dbmod
from hypnos import habits as H
from hypnos import lineage, sleepcmd
from hypnos.night import Night, NightConfig


def two_nights(tmp_path):
    db_path = str(tmp_path / "data.db")
    populate(db_path)
    cfg = NightConfig(trainer_python=FAKE, cycles=2, max_minutes=5)
    Night(db_path, tmp_path, cfg).run()
    Night(db_path, tmp_path, cfg).run()
    return db_path, cfg


def test_plan_finds_contaminated_lineage(tmp_path):
    db_path, _ = two_nights(tmp_path)
    conn = dbmod.open_db(db_path)
    plan = lineage.plan(conn, "session", "s3")  # one of the "add <pkg>" sessions, trained into v1
    assert plan.episodes and plan.affected == [1, 2] and plan.parent is None
    assert not set(plan.episodes) & set(plan.retrain_episodes)
    assert plan.retrain_episodes


def test_habit_plan_includes_pairs_and_evidence(tmp_path):
    db_path, _ = two_nights(tmp_path)
    conn = dbmod.open_db(db_path)
    habit_id = conn.execute("SELECT id FROM habits").fetchone()[0]
    plan = lineage.plan(conn, "habit", str(habit_id))
    assert plan.habits == [habit_id] and plan.pairs and plan.episodes
    with pytest.raises(ValueError):
        lineage.plan(conn, "habit", "999")


def test_lethe_retrains_without_the_data_and_deletes_contaminated(tmp_path):
    db_path, cfg = two_nights(tmp_path)
    conn = dbmod.open_db(db_path)
    plan = lineage.plan(conn, "session", "s3")
    v1_adapter = conn.execute("SELECT adapter_path FROM brains WHERE version = 1").fetchone()[0]
    assert Path(v1_adapter).exists()
    needs, msg = sleepcmd.lethe_apply(db_path, plan)
    assert needs and "excluded" in msg
    report = Night(db_path, tmp_path, cfg, lethe=plan).run()
    rows = {r["version"]: dict(r) for r in conn.execute("SELECT * FROM brains")}
    assert rows[1]["status"] == "forgotten" and rows[2]["status"] == "forgotten"
    assert rows[1]["adapter_path"] is None and not Path(v1_adapter).exists()
    new = rows[3]
    assert new["parent_version"] is None and new["status"] == "promoted" and report["promoted"]
    manifest = json.loads(new["manifest_json"])
    assert not set(manifest["episodes"]) & set(plan.episodes)
    excluded = {r[0] for r in conn.execute("SELECT id FROM episodes WHERE excluded = 1")}
    assert excluded == set(plan.episodes)


def test_failed_retrain_falls_back_to_clean_parent(tmp_path, monkeypatch):
    db_path, cfg = two_nights(tmp_path)
    conn = dbmod.open_db(db_path)
    # Pretend only v2 saw the data: its parent v1 stays clean.
    v2 = json.loads(conn.execute("SELECT manifest_json FROM brains WHERE version = 2").fetchone()[0])
    v2["episodes"] = [999]
    conn.execute("UPDATE brains SET manifest_json = ? WHERE version = 2", (json.dumps(v2),))
    conn.execute("INSERT INTO episodes(id, turn_id, session_id, turn_index, started_at, status, split) "
                 "VALUES(999, 'tx', 'sx', 1, 0, 'ok', 'train')")
    plan = lineage.plan(conn, "session", "sx")
    assert plan.affected == [2] and plan.parent == 1
    sleepcmd.lethe_apply(db_path, plan)
    monkeypatch.setenv("HYPNOS_FAKE_POLICY", "garbage")
    Night(db_path, tmp_path, cfg, lethe=plan).run()
    statuses = {r["version"]: r["status"] for r in conn.execute("SELECT version, status FROM brains")}
    assert statuses[2] == "forgotten" and statuses[3] == "rejected" and statuses[1] == "promoted"


def test_forgotten_habit_is_not_relearned_from_memory(tmp_path):
    conn = dbmod.open_db(tmp_path / "d.db")
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "USER.md").write_text("Always use conventional commits")
    H.refresh(conn, mem)
    habit_id = conn.execute("SELECT id FROM habits").fetchone()[0]
    plan = lineage.plan(conn, "habit", str(habit_id))
    lineage.exclude(conn, plan)
    H.refresh(conn, mem)
    row = conn.execute("SELECT status, excluded FROM habits").fetchone()
    assert row["status"] == "forgotten" and row["excluded"] == 1
    assert conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0] == 1


def test_lethe_slash_needs_a_code(tmp_path):
    db_path, _ = two_nights(tmp_path)
    spawned = []
    text = sleepcmd.lethe_slash("session s3", db_path=db_path, data_dir=tmp_path)
    assert "confirm" in text and "retired" in text
    code = text.rsplit("/lethe confirm ", 1)[1].split()[0]
    assert "unknown or expired" in sleepcmd.lethe_slash("confirm nope", db_path=db_path, data_dir=tmp_path)
    out = sleepcmd.lethe_slash(f"confirm {code}", db_path=db_path, data_dir=tmp_path,
                               spawn=lambda args, d: spawned.append(args) or 77)
    assert "pid 77" in out and spawned == [["run"]]
    conn = dbmod.open_db(db_path)
    assert dbmod.get_meta(conn, "lethe_running")["kind"] == "session"
    assert "Usage" in sleepcmd.lethe_slash("", db_path=db_path, data_dir=tmp_path)
    assert "Nothing matches" in sleepcmd.lethe_slash("search zzzz-not-there", db_path=db_path, data_dir=tmp_path)


def test_diff_and_export(tmp_path):
    db_path, _ = two_nights(tmp_path)
    conn = dbmod.open_db(db_path)
    text = lineage.diff(conn, tmp_path, 1, 2)
    assert text.startswith("Brain v1 vs v2") and "held-out next action" in text
    assert "Need two existing brains" in lineage.diff(conn, tmp_path, 1, 9)
    out = lineage.export(conn, 2, tmp_path / "exports")
    files = {p.name for p in out.iterdir()}
    assert {"README.md", "hypnos_manifest.json", "SHA256SUMS", "adapter_config.json"} <= files
    dump = "".join(p.read_text() for p in out.iterdir() if p.is_file())
    assert "add lodash" not in dump and "pnpm add" not in dump  # no captured data
    manifest = json.loads((out / "hypnos_manifest.json").read_text())
    assert manifest["manifest_hash"] and manifest["data_counts"]["episodes"] >= 0
