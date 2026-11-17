"""Select an independent weather-year campaign without changing base dates."""
from copy import deepcopy
import re


def folder_label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or value in {".", ".."}:
        raise ValueError(f"Invalid disruption folder label: {value!r}")
    return value


def select_disruption(config):
    """Return effective settings; old configurations retain their flat layout.

    A campaign runs one weather year at a time. Independent array tasks select a
    year and optionally one case through top-level config keys, avoiding YAML's
    recursive mapping merge when trying to remove unselected cases.
    """
    settings = deepcopy(config["disruption"])
    years = settings.get("weather_years")
    if years:
        year = config.get("disruption_weather_year", settings["default_weather_year"])
        if year not in years:
            raise ValueError(f"Unknown disruption weather year {year!r}; choose from {list(years)}")
        settings["output_directory"] = (
            f"{settings['output_directory'].rstrip('/')}/{folder_label(year)}"
        )
        donor = years[year]["weather_network"]
        for case in settings["cases"].values():
            case["weather_network"] = donor
        settings["selected_weather_year"] = year
    case = config.get("disruption_case")
    if case:
        if case not in settings["cases"]:
            raise ValueError(f"Unknown disruption case {case!r}; choose from {list(settings['cases'])}")
        settings["cases"] = {case: settings["cases"][case]}
    for name in settings["cases"]:
        folder_label(name)
    return settings
