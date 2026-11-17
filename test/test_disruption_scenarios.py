"""Campaign isolation, reference-calendar preservation and array task planning."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.disruption_scenarios import select_disruption
from scripts.run_disruption_campaign import NEW_YEARS, campaign_tasks, check_inputs, task_targets


@pytest.fixture
def config():
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / "config/config.interconnector_disruption.yaml").read_text(encoding="utf-8"))


def test_each_year_uses_matching_donor_without_changing_dates_or_capacities(config):
    original = deepcopy(config)
    for year in NEW_YEARS:
        selected = select_disruption({**config, "disruption_weather_year": year})
        assert selected["output_directory"] == f"{config['disruption']['output_directory']}/{year}"
        for name, case in selected["cases"].items():
            assert case["weather_network"] == config["disruption"]["weather_years"][year]["weather_network"]
            assert case["interconnector_outages"] == original["disruption"]["cases"][name]["interconnector_outages"]
        assert selected["base_network"] == original["disruption"]["base_network"]
    assert config == original


def test_array_owns_disjoint_case_outputs_and_never_comparison_plots(config):
    tasks = campaign_tasks(config, NEW_YEARS)
    assert len(tasks) == 32
    assert len({task["target"] for task in tasks}) == 32
    for task in tasks:
        assert list(task["settings"]["cases"]) == [task["case"]]
        directory = str(Path(task["target"]).parent).replace("\\", "/")
        assert all(target.startswith(directory + "/") for target in task_targets(task))
        assert all("missing_demand" not in target for target in task_targets(task))
    assert tasks[0]["year"] == "1964-1965"
    assert tasks[-1]["year"] == "1982-1983"


def test_reference_year_uses_base_weather(config):
    first = select_disruption(config)
    assert first["output_directory"].endswith("/2013-2014")
    assert all(case["weather_network"] is None for case in first["cases"].values())


def test_legacy_plot_overlay_retains_old_paths(config):
    config["disruption"]["weather_years"] = None
    assert select_disruption(config)["output_directory"] == config["disruption"]["output_directory"]


@pytest.mark.parametrize("selection", [
    {"disruption_weather_year": "1900-1901"},
    {"disruption_case": "unknown"},
])
def test_invalid_selection_fails(config, selection):
    with pytest.raises(ValueError):
        select_disruption({**config, **selection})


def test_preflight_reports_missing_donor(config, tmp_path):
    base = tmp_path / "base.nc"
    base.touch()
    config["disruption"].update(base_network=str(base), household_shares=None)
    missing = tmp_path / "donor.nc"
    config["disruption"]["weather_years"][NEW_YEARS[0]]["weather_network"] = str(missing)
    tasks = campaign_tasks(config, [NEW_YEARS[0]])
    with pytest.raises(FileNotFoundError, match="donor.nc"):
        check_inputs(tasks)
    missing.touch()
    check_inputs(tasks)


def test_duplicate_tasks_rejected(config):
    with pytest.raises(ValueError, match="overlapping"):
        campaign_tasks(config, [NEW_YEARS[0], NEW_YEARS[0]])


def test_donor_overlays_cover_requested_july_june_calendars(config):
    from datetime import date

    root = Path(__file__).resolve().parents[1]
    for year in NEW_YEARS:
        start, end = year.split("-")
        suffix = f"{start}_{end[2:]}"
        overlay = yaml.safe_load((root / f"config/config.weather_{suffix}.yaml").read_text(encoding="utf-8"))
        assert overlay["snapshots"]["start"] == f"{start}-07-01"
        assert overlay["snapshots"]["end"] == f"{end}-07-01"
        assert overlay["enable"]["drop_leap_day"] is True
        assert overlay["run"]["prefix"] == f"weather_{suffix}"
        donor = config["disruption"]["weather_years"][year]["weather_network"]
        assert donor.startswith(f"resources/{overlay['run']['prefix']}/sector_2025/")
        cutout = overlay["atlite"]["cutouts"][overlay["atlite"]["default_cutout"]]
        assert date.fromisoformat(cutout["time"][0]) <= date(int(start), 7, 1)
        assert date.fromisoformat(cutout["time"][1]) >= date(int(end), 6, 30)
