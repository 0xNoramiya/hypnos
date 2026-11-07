import pytest
from test_night import make_night

from hypnos import db as dbmod
from hypnos import sleepcmd


@pytest.fixture
def slept(tmp_path):
    night, _ = make_night(tmp_path)
    night.run()
    return night


def test_report_and_hypnogram_text(slept):
    text = sleepcmd.report_text(slept.db_path)
    assert "HYPNOS SLEEP STUDY" in text and "PROMOTED" in text
    gram = sleepcmd.hypnogram_text(slept.db_path)
    lines = gram.splitlines()
    assert lines[0].startswith("Hypnogram  night #1")
    assert [line.split("|")[0].strip() for line in lines[1:6]] == ["Wake", "REM", "N1", "N2", "N3"]
    assert "#" in lines[2] and "#" in lines[5]


def test_brain_list(slept):
    text = sleepcmd.brain_list_text(slept.db_path)
    assert "v1" in text and "promoted" in text and "packages (shadow)" in text


def test_sleep_slash(tmp_path):
    db_path = str(tmp_path / "d.db")
    calls = []

    def spawn(args, data_dir):
        calls.append(args)
        return 4242

    out = sleepcmd.sleep_slash("now", db_path=db_path, data_dir=tmp_path, spawn=spawn)
    assert "4242" in out and calls == [["--trigger", "slash"]]
    assert "never slept" in sleepcmd.sleep_slash("status", db_path=db_path, data_dir=tmp_path, spawn=spawn)
    conn = dbmod.open_db(db_path)
    conn.execute("INSERT INTO nights(started_at, status) VALUES(0, 'running')")
    conn.execute("INSERT INTO night_events(night_id, ts, stage, event) VALUES(1, 1, 'N3', 'phase_start')")
    assert "Already asleep" in sleepcmd.sleep_slash("now", db_path=db_path, data_dir=tmp_path, spawn=spawn)
    assert "now: N3" in sleepcmd.sleep_slash("status", db_path=db_path, data_dir=tmp_path, spawn=spawn)
    assert "Waking" in sleepcmd.sleep_slash("cancel", db_path=db_path, data_dir=tmp_path, spawn=spawn)
    assert "Usage" in sleepcmd.sleep_slash("dance", db_path=db_path, data_dir=tmp_path, spawn=spawn)


def test_rollback(slept):
    second = type(slept)(slept.db_path, slept.data_dir, slept.cfg)
    second.run()
    conn = dbmod.open_db(slept.db_path)
    assert [r["status"] for r in conn.execute("SELECT status FROM brains ORDER BY version")] == ["retired", "promoted"]
    message, brain = sleepcmd.rollback(slept.db_path, 1)
    assert "v1" in message and brain["version"] == 1
    assert [r["status"] for r in conn.execute("SELECT status FROM brains ORDER BY version")] == ["promoted", "retired"]
    message, brain = sleepcmd.rollback(slept.db_path, 0)
    assert brain is None and "no brain" in message
    assert "no brain v9" in sleepcmd.rollback(slept.db_path, 9)[0]


def test_night_config_from_settings():
    cfg = sleepcmd.night_config({"base_model": "x/y", "cycles": 2, "night_max_minutes": 5}, tiny=True)
    assert cfg.model == cfg.tiny_model and cfg.cycles == 2 and cfg.max_minutes == 5 and cfg.max_steps == 40 and cfg.effective_batch == 4
