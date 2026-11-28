"""One campaign metrics table for event/year comparisons and summary plots."""
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.analyse_disruption_windows import TIMING_METRICS, select_campaign_windows


def metrics(n, region, start, end, noise=0.1):
    from scripts.outage_helpers import missing_demand, region_balance

    hours = n.snapshots[(n.snapshots >= pd.Timestamp(start)) & (n.snapshots < pd.Timestamp(end))]
    if hours.empty:
        raise ValueError("Empty metric evaluation period.")
    result = {}
    for kind in ("power", "heat"):
        balance = missing_demand(region_balance(n, region, kind, noise)).loc[hours]
        shed = balance.shed_MW
        result.update({f"ens_{kind}_MWh": float(shed.sum()),
                       f"peak_shedding_{kind}_MW": float(shed.max()),
                       f"shedding_hours_{kind}": int(shed.gt(noise).sum())})
    locations = n.generators.bus.map(n.buses.location)
    selected = n.generators.index[locations.eq(region) | n.generators.bus.eq(region)]
    for fuel in ("oil", "gas", "biomass", "hydrogen"):
        names = [name for name, record in n.meta.get("fuel_demand_shedding", {}).items()
                 if record["fuel"] == fuel and name in selected]
        if fuel == "hydrogen":
            names += [name for name in n.meta.get("hydrogen_balance_shedding", {}) if name in selected]
        power = n.generators_t.p.reindex(index=hours, columns=names, fill_value=0).clip(lower=0).sum(axis=1)
        result[f"unserved_{fuel}_MWh"] = float(power.sum())
    names = selected[n.generators.loc[selected, "carrier"].str.startswith("load-shedding")]
    power = n.generators_t.p.reindex(index=hours, columns=names, fill_value=0).clip(lower=0)
    costs = n.get_switchable_as_dense("Generator", "marginal_cost").loc[hours, names]
    result["regional_shedding_cost_EUR"] = float((power * costs).sum().sum())
    price = n.buses_t.marginal_price.loc[hours, region]
    if not np.isfinite(price.to_numpy()).all():
        raise ValueError("Missing regional marginal prices.")
    result["mean_electricity_price_EUR_per_MWh"] = float(price.mean())
    result["max_electricity_price_EUR_per_MWh"] = float(price.max())
    return result


def campaign_metrics(settings):
    import pypsa

    from scripts.disruption_scenarios import scenario_definitions
    root = Path(settings["output_root"])
    campaign = settings["campaign"]
    cases = scenario_definitions(settings)
    records = []
    for variant in settings["variants"]:
        for year in settings["weather_years"]:
            baseline_dir = root / "base_cases" / variant / year
            baseline = pypsa.Network(baseline_dir / "base_weather/network_solved.nc")
            base_summary = pd.read_csv(baseline_dir / "base_weather/dispatch_summary.csv").iloc[0]
            windows = select_campaign_windows(
                pd.read_csv(baseline_dir / "event_windows.csv"), campaign, settings["durations_hours"]
            ).set_index("duration_hours")
            cache = {}
            for case, definition in cases.items():
                directory = root / campaign / variant / year / case
                n = pypsa.Network(directory / "network_solved.nc")
                summary = pd.read_csv(directory / "dispatch_summary.csv").iloc[0]
                window = windows.loc[definition["duration_hours"]]
                event = n.meta.get("disruption_event", {})
                if (event.get("campaign") != campaign or event.get("timing_metric") != TIMING_METRICS[campaign]
                        or pd.Timestamp(event.get("start")) != pd.Timestamp(window.start)
                        or pd.Timestamp(event.get("end")) != pd.Timestamp(window.end)):
                    raise ValueError(f"Solved scenario uses obsolete/different event timing: {directory}.")
                for period, start, end in (("event", window.start, window.end),
                                           ("year", n.snapshots[0], n.snapshots[-1] + pd.Timedelta(hours=1))):
                    key = (str(start), str(end))
                    if key not in cache:
                        cache[key] = metrics(baseline, settings["region"], start, end)
                    values = metrics(n, settings["region"], start, end)
                    base_values = cache[key].copy()
                    if period == "year":
                        for name in ("operating_cost_EUR", "net_emissions_tCO2"):
                            values[f"system_{name}"] = float(summary[name])
                            base_values[f"system_{name}"] = float(base_summary[name])
                    row = {"campaign": campaign, "timing_metric": TIMING_METRICS[campaign], "variant": variant, "weather_year": year, "case": case,
                           "duration_hours": definition["duration_hours"], "period": period,
                           "start": start, "end": end, **values}
                    for name, value in values.items():
                        row[f"baseline_{name}"] = base_values[name]
                        row[f"delta_{name}"] = value - base_values[name]
                    records.append(row)
    return pd.DataFrame(records)


def plot_metrics(table, outputs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    for variant, frame in table.loc[table.period.eq("event")].groupby("variant", sort=False):
        for kind in ("power", "heat"):
            column = f"delta_ens_{kind}_MWh"
            values = frame.pivot(index="case", columns="weather_year", values=column)
            # Preserve scenario/duration order from the common task expansion.
            values = values.reindex(index=frame.case.drop_duplicates(), columns=frame.weather_year.drop_duplicates())
            fig, ax = plt.subplots(figsize=(9, max(6, len(values) * .23)))
            bound = max(float(values.abs().max().max()), 1.0)
            im = ax.imshow(values.to_numpy(), aspect="auto", cmap="RdBu_r", vmin=-bound, vmax=bound)
            ax.set_xticks(range(len(values.columns)), values.columns, rotation=35, ha="right")
            ax.set_yticks(range(len(values)), values.index, fontsize=7)
            ax.set_title(f"{variant}: extra DK2 unserved {kind} during selected event")
            fig.colorbar(im, ax=ax, label="MWh relative to matching baseline")
            path = Path(outputs[f"{variant}_{kind}"])
            path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(path, dpi=180, bbox_inches="tight")
            plt.close(fig)


if __name__ == "__main__":
    table = campaign_metrics(snakemake.params.settings)
    Path(snakemake.output.table).parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(snakemake.output.table, index=False)
    plot_metrics(table, snakemake.output)
