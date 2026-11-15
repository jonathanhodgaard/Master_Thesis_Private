# SPDX-License-Identifier: MIT
"""Compare saved perfect-foresight and rolling electricity/heat dispatch.

Run from the repository root (no optimization is performed)::

    python scripts/plot_disruption_dispatch.py --region DK0

In the current ten-node networks, model cluster DK0 is eastern Denmark (the
usual bidding zone DK2); model cluster DK2 is western Denmark. ``--region``
matches network bus names, not bidding-zone names. Use ``--title`` to set the
geographical label. Defaults plot the first two weeks of the rolling result.

Outputs: separate PNG/PDF comparisons for electricity, urban central heat,
urban decentral heat and rural heat; hourly CSV data; validation/provenance JSON.
All dispatch and demand values in the CSV files are MW. Positive dispatch
supplies the selected buses; negative dispatch is charging, export or venting.
"""

import argparse
import gc
import json
import logging
import math
from pathlib import Path
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pypsa


ROOT = Path(__file__).resolve().parents[1]
SECTORS = {
    "electricity": ("Electricity", {"AC", "DC", "low voltage"}),
    "urban-central": ("Urban central heat", {"urban central heat"}),
    "urban-decentral": ("Urban decentral heat", {"urban decentral heat"}),
    "rural": ("Rural heat", {"rural heat"}),
}
COLORS = {
    "Onshore wind": "#238b8e", "Offshore wind": "#68c4c7",
    "Solar PV": "#efc84a", "Solar thermal": "#f29d38",
    "Air-source heat pump": "#3c97a3", "Ground-source heat pump": "#76beb8",
    "Heat pump": "#4baba8", "Resistive heater": "#e6bb49",
    "Biomass CHP": "#6a984e", "Biomass boiler": "#a0b878",
    "Gas CHP": "#bd7067", "Gas boiler": "#d98b62",
    "Oil CHP": "#b3658b", "Oil boiler": "#da9dbb",
    "Oil-fired generation": "#d98ba2", "Gas turbines": "#bb7662",
    "Coal CHP": "#777b80", "Coal-fired generation": "#777b80",
    "Lignite-fired generation": "#9c825f", "Waste CHP": "#b59872",
    "Waste boiler": "#d0bda0", "Tank storage": "#6485b7",
    "Pit storage": "#97abd0", "Aquifer storage": "#75859b",
    "Battery": "#547ca7", "Pumped hydro": "#416889",
    "Hydropower": "#5486bf", "Hydrogen generation": "#b098d1",
    "Net imports / exports": "#8c80b6", "Heat venting": "#b9b9b9",
    "Load shedding": "#df2935",
}


def technology_label(carrier):
    """Use common labels across cases, aggregating CHP capture variants."""
    c = carrier.lower()
    if "load-shedding" in c:
        return "Load shedding"
    if "heat vent" in c:
        return "Heat venting"
    if "water tank" in c:
        return "Tank storage"
    if "water pit" in c:
        return "Pit storage"
    if "ates" in c or "aquifer" in c:
        return "Aquifer storage"
    if "battery" in c:
        return "Battery"
    if c.startswith("offwind"):
        return "Offshore wind"
    if c.startswith("onwind"):
        return "Onshore wind"
    if "solar thermal" in c:
        return "Solar thermal"
    if c.startswith("solar"):
        return "Solar PV"
    if "heat pump" in c:
        if "air" in c:
            return "Air-source heat pump"
        if "ground" in c:
            return "Ground-source heat pump"
        return "Heat pump"
    if "resistive" in c:
        return "Resistive heater"
    if "chp" in c or "boiler" in c:
        fuel = next((name for name in ("biomass", "waste", "gas", "oil", "coal", "hydrogen", "methanol")
                     if name in c), None)
        if fuel:
            return f"{fuel.capitalize()} {'CHP' if 'chp' in c else 'boiler'}"
    if "fuel cell" in c or "h2 turbine" in c or "hydrogen turbine" in c:
        return "Hydrogen generation"
    if "ocgt" in c or "ccgt" in c:
        return "Hydrogen generation" if "h2" in c else "Gas turbines"
    if c in {"hydro", "ror"}:
        return "Hydropower"
    if c == "phs":
        return "Pumped hydro"
    if c in {"oil", "coal", "lignite", "biomass", "nuclear"}:
        return f"{c.capitalize()}-fired generation"
    return re.sub(r"^(urban central|urban decentral|rural)\s+", "", carrier).strip() or "Other"


def output_flow(n, component, attr, snapshots, names):
    """PyPSA can omit output columns whose values are all the default zero."""
    frame = n.components[component].dynamic.get(attr, pd.DataFrame(index=n.snapshots))
    result = frame.reindex(index=snapshots, columns=names, fill_value=0)
    if not np.isfinite(result.to_numpy()).all():
        raise ValueError(f"Missing/nonfinite saved output {component}.{attr}")
    return result


def extract_dispatch(n, snapshots, region, sector, tolerance):
    carriers = SECTORS[sector][1]
    local = n.buses.index.to_series().eq(region) | n.buses.index.to_series().str.startswith(region + " ")
    buses = n.buses.index[local & n.buses.carrier.isin(carriers)]
    if buses.empty:
        raise ValueError(f"No {sector} buses match model region {region!r}")
    bus_set = set(buses)
    sector_buses = set(n.buses.index[n.buses.carrier.isin(carriers)])
    flows = {}

    def add(label, values):
        flows[label] = flows.get(label, pd.Series(0.0, index=snapshots)) + values

    for component in ("Generator", "Store", "StorageUnit"):
        table = n.components[component].static
        names = table.index[table.bus.isin(buses)]
        frame = output_flow(n, component, "p", snapshots, names)
        for name in names:
            add(technology_label(table.at[name, "carrier"]), frame[name] * table.at[name, "sign"])

    names = n.loads.index[n.loads.bus.isin(buses)]
    loads = n.get_switchable_as_dense("Load", "p_set", snapshots)[names]
    fixed = -loads.mul(n.loads.loc[names, "sign"], axis=1).sum(axis=1)
    demand = fixed.copy()
    for component in ("Link", "Line", "Transformer"):
        c = n.components[component]
        ports = [col for col in c.static if re.fullmatch(r"bus\d+", col)]
        names = c.static.index[c.static[ports].isin(buses).any(axis=1)]
        frames = {port: output_flow(n, component, "p" + port[3:], snapshots, names)
                  for port in ports}
        for name in names:
            row = c.static.loc[name]
            endpoints = [row[port] for port in ports if row[port]]
            injection = pd.Series(0.0, index=snapshots)
            for port in ports:
                if row[port] in bus_set:
                    injection -= frames[port][name]
            if all(bus in bus_set for bus in endpoints):
                # Distribution and household delivery are internal transfers.
                # Sum both ends, retaining only any conversion/network loss.
                demand -= injection
            elif any(bus in sector_buses - bus_set for bus in endpoints):
                add("Net imports / exports", injection)
            else:
                label = technology_label(row.get("carrier", component))
                if label in {"Tank storage", "Pit storage", "Aquifer storage", "Battery", "Pumped hydro", "Heat venting"}:
                    add(label, injection)
                else:
                    add(label, injection.clip(lower=0))
                    # Electricity-to-heat, electrolysis and other consumption
                    # belong to total use, rather than fixed final demand.
                    demand -= injection.clip(upper=0)
    dispatch = pd.DataFrame(flows, index=snapshots).fillna(0)
    error = float((dispatch.sum(axis=1) - demand).abs().max())
    if error > tolerance:
        raise ValueError(f"{region} {sector} balance error {error:.6g} MW exceeds {tolerance:g} MW")
    logging.info("%s: %d buses; max balance error %.6g MW", sector, len(buses), error)
    return {"dispatch": dispatch, "demand": demand, "fixed": fixed,
            "checks": {"buses": list(buses), "max_balance_error_MW": error}}


def plot_comparison(sector, cases, snapshots, end, output, title, colors, min_peak):
    columns = sorted(set().union(*(case["dispatch"].columns for case in cases)))
    columns = [col for col in columns if max(case["dispatch"].get(col, pd.Series([0])).abs().max()
                                           for case in cases) >= min_peak]
    preferred = list(COLORS)
    columns.sort(key=lambda col: (preferred.index(col) if col in preferred else len(preferred), col))
    x = snapshots.append(pd.DatetimeIndex([end]))

    def steps(values):
        values = np.asarray(values) / 1000
        return np.r_[values, values[-1]]

    fig, axes = plt.subplots(2, 1, figsize=(15, 8.5), sharex=True, sharey=True)
    for ax, case in zip(axes, cases):
        positive, negative = np.zeros(len(x)), np.zeros(len(x))
        data = case["dispatch"].reindex(columns=columns, fill_value=0)
        for col in columns:
            up, down = steps(data[col].clip(lower=0)), steps(data[col].clip(upper=0))
            ax.fill_between(x, positive, positive + up, color=colors[col], linewidth=0, step="post")
            ax.fill_between(x, negative, negative + down, color=colors[col], linewidth=0, step="post")
            positive += up
            negative += down
        ax.plot(x, steps(case["demand"]), color="#20262b", linewidth=1.2, drawstyle="steps-post")
        ax.plot(x, steps(case["fixed"]), color="#20262b", linewidth=1.0, linestyle="--", drawstyle="steps-post")
        ax.axhline(0, color="#52616b", linewidth=0.6)
        ax.set_title(case["label"], loc="left", fontweight="bold", fontsize=12)
        ax.set_ylabel("Electricity [GW]" if sector == "electricity" else "Heat [GW thermal]")
        ax.grid(axis="y", alpha=0.18)
        ax.set_axisbelow(True)
        case["dispatch"].assign(fixed_demand_MW=case["fixed"], total_use_MW=case["demand"]).to_csv(
            output / f"{sector}_{case['key']}.csv", index_label="snapshot")
    days = max(1, (end - snapshots[0]).total_seconds() / 86400)
    axes[-1].xaxis.set_major_locator(mdates.DayLocator(interval=max(1, math.ceil(days / 14))))
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    axes[-1].set_xlim(snapshots[0], end)
    handles = [Patch(color=colors[col], label=col) for col in columns]
    handles += [Line2D([0], [0], color="#20262b", label="Total use"),
                Line2D([0], [0], color="#20262b", linestyle="--", label="Fixed demand")]
    legend_rows = math.ceil(len(handles) / 4)
    bottom = max(0.19, 0.11 + legend_rows * 0.035)
    fig.suptitle(f"{title} | {SECTORS[sector][0]} dispatch", x=0.07, ha="left",
                 fontsize=17, fontweight="bold", y=0.98)
    fig.text(0.07, 0.932, f"{snapshots[0]:%d %b %Y} – {snapshots[-1]:%d %b %Y}; matching hours and vertical scales.", color="#52616b")
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.05), ncol=4, frameon=False, fontsize=9)
    fig.text(0.07, 0.015, "Solid: total use including conversion/network losses. Dashed: fixed final demand. "
             "Below zero: storage charging, net exports or heat venting.\n"
             "Household technology buses are aggregated with their heat bus; internal heat delivery is counted once.",
             fontsize=8.5, color="#52616b")
    fig.subplots_adjust(left=0.07, right=0.98, top=0.88, bottom=bottom, hspace=0.25)
    for extension in ("png", "pdf"):
        fig.savefig(output / f"{sector}_perfect_foresight_vs_rolling.{extension}", dpi=180, facecolor="white")
    plt.close(fig)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, default=ROOT / "results/sector_2025/networks/base_s_10___2025.nc")
    parser.add_argument("--rolling", type=Path, default=ROOT / "results/disruptions/sector_2025_rolling/base_weather/network_solved.nc")
    parser.add_argument("--region", default="DK0", help="Model bus prefix (DK0 is eastern Denmark in the current ten-node network).")
    parser.add_argument("--title", help="Geographical display label; defaults to the model region.")
    parser.add_argument("--start", help="First included snapshot; defaults to the first saved rolling snapshot.")
    parser.add_argument("--end", help="Exclusive end timestamp; defaults to at most 14 days after start.")
    parser.add_argument("--sectors", nargs="+", choices=list(SECTORS), default=list(SECTORS))
    parser.add_argument("--output-dir", type=Path, help="Defaults to a region-specific plots folder beside the rolling network.")
    parser.add_argument("--min-peak-mw", type=float, default=0.1, help="Hide smaller technologies in plots; CSVs retain them.")
    parser.add_argument("--balance-tolerance-mw", type=float, default=0.01)
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "figure.autolayout": False, "figure.constrained_layout.use": False})
    if args.min_peak_mw < 0 or args.balance_tolerance_mw <= 0:
        raise ValueError("Require nonnegative plot threshold and positive balance tolerance")
    rolling = pypsa.Network(args.rolling)
    if rolling.objective is None or not np.isfinite(rolling.objective):
        raise ValueError("The rolling network must contain a solved dispatch")
    if not isinstance(rolling.snapshots, pd.DatetimeIndex) or not rolling.snapshots.is_unique or not rolling.snapshots.is_monotonic_increasing:
        raise ValueError("Require unique chronological single-period datetime snapshots")
    start = pd.Timestamp(args.start) if args.start else rolling.snapshots[0]
    interval = rolling.snapshots[1] - rolling.snapshots[0] if len(rolling.snapshots) > 1 else pd.Timedelta(hours=1)
    end = pd.Timestamp(args.end) if args.end else min(start + pd.Timedelta(days=14), rolling.snapshots[-1] + interval)
    if start not in rolling.snapshots or end <= start or end > rolling.snapshots[-1] + interval:
        raise ValueError("Requested dates must lie within the saved rolling period; end is exclusive")
    snapshots = rolling.snapshots[(rolling.snapshots >= start) & (rolling.snapshots < end)]
    if len(snapshots) > 1 and not (snapshots.to_series().diff().dropna() == interval).all():
        raise ValueError("The selected rolling snapshots contain gaps")
    output = args.output_dir or args.rolling.parent / "plots" / f"dispatch_{re.sub(r'[^A-Za-z0-9_-]+', '_', args.region)}"
    output.mkdir(parents=True, exist_ok=True)
    rolling_label = "Rolling horizon"
    settings = rolling.meta.get("rolling_dispatch", {})
    if "horizon" in settings and "overlap" in settings:
        rolling_label += f": {settings['horizon']}-step horizon, {settings['overlap']}-step overlap"
    results = {sector: [extract_dispatch(rolling, snapshots, args.region, sector, args.balance_tolerance_mw)]
               for sector in args.sectors}
    for cases in results.values():
        cases[0].update(key="rolling", label=rolling_label)
    provenance = {"region": args.region, "start": str(start), "end_exclusive": str(end),
                  "snapshots": len(snapshots), "units": "MW", "sectors": {},
                  "rolling_storage_msv": rolling.meta.get("disruption_storage_msv", []),
                  "sources": {key: {"path": str(path.resolve()), "modified_unix": path.stat().st_mtime}
                              for key, path in (("base", args.base), ("rolling", args.rolling))}}
    del rolling
    gc.collect()
    base = pypsa.Network(args.base)
    if base.objective is None or not np.isfinite(base.objective):
        raise ValueError("The base network must contain a solved perfect-foresight dispatch")
    if len(snapshots.difference(base.snapshots)):
        raise ValueError("The perfect-foresight network lacks requested snapshots")
    for sector, cases in results.items():
        comparison = extract_dispatch(base, snapshots, args.region, sector, args.balance_tolerance_mw)
        comparison.update(key="perfect_foresight", label="Perfect foresight: full-year base optimization")
        cases.insert(0, comparison)
        provenance["sectors"][sector] = {case["key"]: case["checks"] for case in cases}
        provenance["sectors"][sector]["max_fixed_demand_difference_MW"] = float((cases[0]["fixed"] - cases[1]["fixed"]).abs().max())
    del base
    gc.collect()
    labels = sorted(set().union(*(case["dispatch"].columns for cases in results.values() for case in cases)))
    colors = {label: COLORS.get(label, matplotlib.colormaps["tab20"](i % 20)) for i, label in enumerate(labels)}
    for sector, cases in results.items():
        plot_comparison(sector, cases, snapshots, end, output, args.title or f"Model region {args.region}", colors, args.min_peak_mw)
    (output / "dispatch_comparison_checks.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    logging.info("Saved %d dispatch comparisons and their data to %s", len(results), output.resolve())


if __name__ == "__main__":
    main()
