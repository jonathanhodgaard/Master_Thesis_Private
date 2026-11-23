"""New donor campaigns keep case settings and isolate every model/year/case."""
from copy import deepcopy
from pathlib import Path
import re

import pytest
import yaml

from scripts.run_disruption_campaign import NEW_YEARS, campaign_tasks, load_config, check_inputs

ROOT = Path(__file__).resolve().parents[1]
YEARS = ["1988-1989", "1998-1999"]


@pytest.mark.parametrize("scenario", ["sector_2025", "sector_2035_w_V2G", "sector_2035_wo_V2G"])
def test_new_arrays_select_correct_donors_and_preserve_case_settings(scenario):
    suffix = "" if scenario == "sector_2025" else scenario.removeprefix("sector")
    paths = [ROOT / "config/config.interconnector_disruption.yaml"]
    if suffix:
        paths.append(ROOT / f"config/config.interconnector_disruption{suffix}.yaml")
    config = load_config(paths)
    original = deepcopy(config)
    reference = campaign_tasks(config, ["2013-2014"])
    batch = ROOT / ("run_disruption_array.sbatch" if not suffix else f"run_disruption_array{suffix}.sbatch")
    text = batch.read_text(encoding="utf-8")
    assert NEW_YEARS == ["1965-1966", "1968-1969", *YEARS]
    assert list(config["disruption"]["weather_years"]) == ["2013-2014", *NEW_YEARS]
    if suffix:
        years = re.search(r"--years ([\d,-]+)", text).group(1).split(",")
        assert years == ["2013-2014", *NEW_YEARS]
        assert "#SBATCH --array=0-39%4" in text
    else:
        assert "#SBATCH --array=0-31%4" in text
    tasks = campaign_tasks(config, YEARS)
    assert len(tasks) == 16
    assert len({task["target"] for task in tasks}) == 16
    assert "--run-year" not in text
    if suffix:
        assert f"config/config.{scenario}.yaml" in text
        assert f"config/config.interconnector_disruption{suffix}.yaml" in text
        assert "--rerun-incomplete" in text
    horizon = 2025 if scenario == "sector_2025" else 2035
    for index, task in enumerate(tasks):
        year = YEARS[index // 8]
        label = "1988_89" if year == YEARS[0] else "1998_99"
        settings = task["settings"]
        donor = settings["cases"][task["case"]]["weather_network"]
        assert task["year"] == year
        assert donor == f"resources/weather_{label}/{scenario}/networks/base_s_10___{horizon}.nc"
        assert task["target"] == (
            f"results/disruptions/{scenario}_interconnector_disruption/"
            f"{year}/{task['case']}/network_solved.nc"
        )
        assert settings["planning_horizon"] == horizon
        reference_settings = reference[index % 8]["settings"]
        for key in ("base_network", "household_shares", "rolling_horizon", "load_shedding"):
            assert settings[key] == reference_settings[key]
        case = deepcopy(settings["cases"][task["case"]])
        case["weather_network"] = None
        assert case == reference_settings["cases"][task["case"]]
    assert config == original


def test_missing_second_donor_is_reported_before_submission(tmp_path):
    config = load_config([ROOT / "config/config.interconnector_disruption.yaml"])
    for key in ("base_network", "household_shares"):
        path = tmp_path / key
        path.touch()
        config["disruption"][key] = str(path)
    for year in YEARS:
        path = tmp_path / f"{year}.nc"
        config["disruption"]["weather_years"][year]["weather_network"] = str(path)
    (tmp_path / "1988-1989.nc").touch()
    tasks = campaign_tasks(config, YEARS)
    with pytest.raises(FileNotFoundError, match="1998-1999.nc"):
        check_inputs(tasks)
    (tmp_path / "1998-1999.nc").touch()
    check_inputs(tasks)


def test_weather_overlays_cover_both_new_donor_calendars():
    for year, label in zip(YEARS, ["1988_89", "1998_99"]):
        overlay = yaml.safe_load((ROOT / f"config/config.weather_{label}.yaml").read_text())
        start, end = year.split("-")
        assert overlay["snapshots"] == {
            "start": f"{start}-07-01", "end": f"{end}-07-01", "inclusive": "left"
        }
