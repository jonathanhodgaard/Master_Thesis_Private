"""2035 campaign parity, output isolation and one-year sequential execution."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import run_disruption_campaign as campaign

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "config/config.interconnector_disruption.yaml"


@pytest.mark.parametrize("variant", ["w_V2G", "wo_V2G"])
def test_v2g_array_has_all_five_years_and_independent_case_tasks(variant):
    import re

    text = (ROOT / f"run_disruption_array_2035_{variant}.sbatch").read_text()
    years = re.search(r"--years ([\d,-]+)", text).group(1).split(",")
    assert years == ["2013-2014", *campaign.NEW_YEARS]
    assert "#SBATCH --array=0-39%4" in text
    assert "--run-year" not in text
    assert f"config/config.sector_2035_{variant}.yaml" in text
    assert f"config/config.interconnector_disruption_2035_{variant}.yaml" in text
    config = campaign.load_config([COMMON, ROOT / f"config/config.interconnector_disruption_2035_{variant}.yaml"])
    tasks = campaign.campaign_tasks(config, years)
    assert len(tasks) == 40
    assert len({task["target"] for task in tasks}) == 40
    assert [tasks[i]["year"] for i in (0, 8, 16, 24, 32)] == years
    assert all(f"sector_2035_{variant}_interconnector_disruption/" in task["target"] for task in tasks)


@pytest.mark.parametrize("variant", ["w_V2G", "wo_V2G"])
def test_2035_inherits_all_2025_assumptions_and_uses_matching_inputs(variant):
    base = campaign.load_config([COMMON])["disruption"]
    overlay = ROOT / f"config/config.interconnector_disruption_2035_{variant}.yaml"
    config = campaign.load_config([COMMON, overlay])
    current = config["disruption"]
    for key in ("cases", "rolling_horizon", "fuel_backup", "heat_vent", "load_shedding",
                "low_voltage_capacity_multipliers"):
        assert current[key] == base[key]
    # 2035 intentionally uses its own scarcity threshold; all other plot
    # settings still inherit from the common campaign.
    expected_plotting = deepcopy(base["plotting"])
    expected_plotting["heatmap"]["scarcity_price"] = 2000
    assert current["plotting"] == expected_plotting
    assert f"sector_2035_{variant}/" in current["base_network"]
    tasks = campaign.campaign_tasks(config, ["2013-2014", *campaign.NEW_YEARS])
    assert len(tasks) == 40
    assert len({task["target"] for task in tasks}) == 40
    assert all(task["settings"]["planning_horizon"] == 2035 for task in tasks)
    for task in tasks:
        donor = task["settings"]["cases"][task["case"]]["weather_network"]
        assert donor is None if task["year"] == "2013-2014" else f"/sector_2035_{variant}/" in donor
    native = campaign.load_config([COMMON, overlay, ROOT / f"config/config.dispatch_2035_{variant}_native_low_voltage.yaml"])
    assert native["disruption"]["output_directory"] != current["output_directory"]
    assert native["disruption"]["low_voltage_capacity_multipliers"] == {"FI": 1.0, "SE": 1.0}


def test_one_year_runs_in_case_order_and_stops_at_first_failure(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["campaign", "--configfiles", str(COMMON),
                                      "--years", "2013-2014", "--run-year"])
    monkeypatch.delenv("SLURM_ARRAY_TASK_ID", raising=False)
    checked, launched = [], []
    monkeypatch.setattr(campaign, "check_inputs", lambda tasks: checked.extend(tasks))

    def run(command, check):
        assert check
        launched.append(command)
        if len(launched) == 2:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(campaign.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        campaign.main()
    assert len(checked) == 8
    assert len(launched) == 2
    assert [cmd[cmd.index("--task-index") + 1] for cmd in launched] == ["0", "1"]


def test_one_year_rejects_multiple_weather_years(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["campaign", "--configfiles", str(COMMON),
                                      "--years", "2013-2014,1965-1966", "--run-year"])
    monkeypatch.delenv("SLURM_ARRAY_TASK_ID", raising=False)
    with pytest.raises(SystemExit):
        campaign.main()
