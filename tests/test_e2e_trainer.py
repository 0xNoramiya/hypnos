"""One real night on a tiny model. Opt-in: HYPNOS_E2E_TRAINER=/path/to/trainer/python (torch, transformers, peft)."""

import os

import pytest
from test_night import populate

from hypnos.night import Night, NightConfig

TRAINER = os.environ.get("HYPNOS_E2E_TRAINER")


@pytest.mark.skipif(not TRAINER, reason="set HYPNOS_E2E_TRAINER to a trainer interpreter")
def test_real_tiny_night(tmp_path):
    db_path = str(tmp_path / "data.db")
    populate(db_path)
    cfg = NightConfig(trainer_python=TRAINER, tiny=True, cycles=2, max_minutes=30, max_steps=40,
                      max_heldout=6, probes_per_habit=4)
    report = Night(db_path, tmp_path, cfg).run()
    assert report["version"] == 1
    assert (tmp_path / "brains" / "v1" / "adapter_config.json").exists()
    assert report["exam"]["candidate"]["probe_n"] == 4
    assert report["train"]["sft_steps"] > 0
