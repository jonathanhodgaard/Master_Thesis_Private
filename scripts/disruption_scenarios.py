"""Resolve shared baselines and expand a small disruption scenario matrix."""
from copy import deepcopy
from itertools import combinations
import json
import re


def folder_label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"Invalid disruption folder label: {value!r}")
    return value


def scenario_definitions(settings):
    campaign = settings["campaign"]
    if campaign == "baseline":
        return {"base_weather": {}}
    cases = {}
    if campaign == "interconnector":
        connections = settings["interconnectors"]
        for count in range(1, len(connections) + 1):
            for names in combinations(connections, count):
                assets = {key: [asset for name in names for asset in connections[name].get(key, [])]
                          for key in ("lines", "links")}
                for hours in settings["durations_hours"]:
                    cases[f"{'_'.join(names)}_{hours}h"] = {
                        "duration_hours": hours, "interconnector_outages": [assets]}
    elif campaign == "fuel":
        for fuel in settings["fuels"]:
            for reduction in settings["reductions"]:
                if not 0 < reduction <= 1:
                    raise ValueError("Fuel reductions must lie in (0, 1].")
                for hours in settings["durations_hours"]:
                    name = f"{fuel}_{round(reduction * 100)}pct_{hours}h"
                    cases[name] = {"duration_hours": hours, "fuel_link_disruptions": [
                        {"fuel": fuel, "reduction": reduction, "locations": [settings["region"]]}]}
    else:
        raise ValueError(f"Unknown campaign {campaign!r}.")
    return cases


def select_disruption(config):
    settings = deepcopy(config["disruption"])
    variant = str(config.get("disruption_variant", settings["default_variant"]))
    year = config.get("disruption_weather_year", settings["default_weather_year"])
    if variant not in settings["variants"] or year not in settings["weather_years"]:
        raise ValueError(f"Unknown network variant/weather year: {variant}/{year}.")
    variant, year = folder_label(variant), folder_label(year)
    network = settings["variants"][variant]
    settings.update(network)
    settings["selected_variant"] = variant
    settings["selected_weather_year"] = year
    root = settings["output_root"].rstrip("/")
    if root in {"results", "resources", "."} or not root.startswith("results/"):
        raise ValueError("Use a dedicated new directory under results/ for output_root.")
    if ".." in root.split("/"):
        raise ValueError("output_root cannot contain parent traversal.")
    settings["baseline_directory"] = f"{root}/base_cases/{variant}/{year}"
    donor = settings["weather_years"][year]["network"]
    settings["weather_network"] = donor.format(**network) if donor else None
    settings["output_directory"] = (settings["baseline_directory"] if settings["campaign"] == "baseline"
                                    else f"{root}/{settings['campaign']}/{variant}/{year}")
    settings["cases"] = scenario_definitions(settings)
    case = config.get("disruption_case")
    if case:
        if case not in settings["cases"]:
            raise ValueError(f"Unknown case {case!r}.")
        settings["cases"] = {case: settings["cases"][case]}
    for name in settings["cases"]:
        folder_label(name)
    return settings


def common_policy(settings, solving, config):
    """Settings that must agree between a prepared baseline and every event."""
    keys = ("base_network", "household_shares", "weather_network", "planning_horizon",
            "biomass_import_capacity_MW", "heat_vent", "load_shedding", "rolling_horizon")
    context = {key: config.get(key) for key in ("sector", "industry", "electricity", "biomass", "foresight")}
    policy = {**{key: settings.get(key) for key in keys}, "solving": solving, "model_context": context}
    # NetCDF metadata uses JSON: normalise numeric YAML keys and tuple values too.
    return json.loads(json.dumps(policy, default=str))
