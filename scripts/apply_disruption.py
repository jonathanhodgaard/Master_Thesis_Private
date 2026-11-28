"""Apply one scenario to a shared prepared network using baseline-selected dates."""
from copy import deepcopy
from pathlib import Path

import pandas as pd

from scripts.interconnector_disruption import apply_interconnector_outages
from scripts.analyse_disruption_windows import select_campaign_windows
from scripts.fuel_link_disruption import apply_fuel_link_disruptions


def apply_scenario(n, definition, windows, case, campaign):
    windows = select_campaign_windows(windows, campaign)
    selected = windows.loc[windows.duration_hours.eq(definition["duration_hours"])]
    if len(selected) != 1:
        raise ValueError("Expected exactly one selected window for the scenario duration.")
    window = selected.iloc[0]
    start, end = pd.Timestamp(window.start), pd.Timestamp(window.end)
    if end - start != pd.Timedelta(hours=definition["duration_hours"]):
        raise ValueError("Event timing duration does not match the scenario.")
    events = deepcopy(definition)
    for kind in ("interconnector_outages", "fuel_link_disruptions"):
        for event in events.get(kind, []):
            event.update(name=case, start=str(start), end=str(end))
    apply_interconnector_outages(n, events.get("interconnector_outages", []))
    apply_fuel_link_disruptions(n, events.get("fuel_link_disruptions", []))
    rows = []
    for event in n.meta.get("interconnector_outages", []):
        for component, key, capacity in (("Line", "lines", "s_nom"), ("Link", "links", "p_nom")):
            for asset in event[key]:
                rows.append({"event": case, "component": component, "asset": asset,
                             "start": str(start), "end": str(end), "reduction": 1.0,
                             "nominal_capacity_MW": float(n.components[component].static.at[asset, capacity])})
    for event in n.meta.get("fuel_link_disruptions", []):
        rows.append({"event": case, "component": "Link", "asset": event["link"],
                     "fuel": event["fuel"],
                     "location": str(n.buses.at[event["bus1"], "location"]),
                     "start": str(start), "end": str(end),
                     "reduction": event["reduction"], "nominal_capacity_MW": event["p_nom_MW"],
                     "normal_max_capacity_MW": event["normal_max_capacity_MW"],
                     "restricted_max_capacity_MW": event["restricted_max_capacity_MW"]})
    n.meta["disruption_event"] = {"case": case, "start": str(start), "end": str(end),
                                   "duration_hours": int(window.duration_hours)}
    n.meta["disruption_event"].update(campaign=campaign, timing_metric=window.timing_metric)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import pypsa

    n = pypsa.Network(snakemake.input.net)
    windows = pd.read_csv(snakemake.input.windows, dtype={"variant": str, "weather_year": str})
    if not (windows.variant.eq(snakemake.params.variant).all()
            and windows.weather_year.eq(snakemake.params.weather_year).all()):
        raise ValueError("Event windows belong to a different baseline.")
    report = apply_scenario(n, snakemake.params.definition, windows, snakemake.wildcards.case, snakemake.params.campaign)
    Path(snakemake.output.net).parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(snakemake.output.report, index=False)
    n.export_to_netcdf(snakemake.output.net)
