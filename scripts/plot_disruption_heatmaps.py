# SPDX-License-Identifier: MIT
"""Resilience heatmaps: interconnector disruption case (rows) x weather year (columns).

Two Snakemake rules (rules/plot_disruption.smk) use this script:

  disruption_case_metrics   one solved case -> {weather_year}/{case}/resilience_metrics.csv
  plot_disruption_heatmaps  all metric files -> {output_directory}/plots/heatmaps/heatmap_*

Metrics per case, region = plotting.region (default DK2 = bus DK0 0AC). All regional
metrics use one fixed window for every case (plotting.heatmap.window, default
1 Dec - 1 Mar, end exclusive = the longest outage window):

  ens_power_GWh, ens_heat_GWh   regional unserved electricity / heat in the window
  price_normal_EUR_per_MWh      regional electricity price weighted by regional
                                electricity demand, over the hours with a price below
                                plotting.heatmap.scarcity_price (default 1000 EUR/MWh)
  scarcity_hours                hours with a price at or above scarcity_price (load
                                shedding sets the price, in the region or, via the
                                cables, in a neighbour); table only
  shedding_hours_power          hours with electricity load shedding in the region
                                (the region's own scarcity)
  scarcity_hours_no_shedding    scarcity-priced hours WITHOUT shedding in the region:
                                a neighbour's shortage priced in over the cables, or
                                storage carrying a nearby shedding hour's value over
  consumer_cost_MEUR            sum over hours of min(price, price_cap) x served regional
                                electricity demand (demand - load shedding: consumers pay
                                only for what they get). price_cap = plotting.heatmap.
                                price_cap (default 4000 EUR/MWh, EU day-ahead maximum).
  consumer_cost_uncapped_MEUR   same with the model price (scarcity hours near VoLL)
  consumer_cost_{normal,scarcity}_MEUR  uncapped, split: hours below / at or above
                                scarcity_price
  voll_cost_MEUR                regional load shedding x its VoLL in the window
                                (electricity and heat shedding generators on the
                                region's buses, their marginal cost = VoLL), M€
  dispatch_cost_excl_shedding_MEUR  system-wide operating cost of the whole run
                                (dispatch_summary.csv, operating_cost_EUR) minus the
                                load-shedding cost. NaN without a summary file.
  plus extra columns for the summary table (peaks, hours, mean/median/max price, CO2).

Normal prices stay below ~300 EUR/MWh in 2025 (~800 EUR/MWh in 2035, higher CO2 price)
and scarcity prices are near VoLL (thousands of EUR/MWh) with nothing in between, so any
cut-off in that gap gives the same split (2035 overlays use 2000 EUR/MWh).

Heatmaps (one PNG per metric, no titles; the file name says what): each cell minus the
no-outage case (plotting.heatmap.reference_case) of the SAME weather year = the outage
effect alone. The no-outage row is left out (all zero). The normal price is shown as a
% change, the others as differences. resilience_metrics_<label>.csv also holds the
absolute values and the differences to the reference cell (reference_case in the
reference weather year).

Cells without a solved network are grey and marked "n/s".
"""
import logging
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.outage_helpers import missing_demand, region_balance  # noqa: E402

logger = logging.getLogger(__name__)

SHEDDING_CARRIERS = ("load-shedding-electricity", "load-shedding-heat")
DEFAULT_WINDOW = ("2013-12-01", "2014-03-01")   # end exclusive
DEFAULT_SCARCITY_PRICE = 1000.0                 # EUR/MWh
# EU day-ahead harmonised maximum clearing price (SDAC); check the value in force
DEFAULT_PRICE_CAP = 4000.0                      # EUR/MWh
# Gap check: hours priced within this factor of the cut-off (cut-off / 2 ... cut-off x 2)
# make the normal/scarcity split depend on the exact cut-off -> warning in the log.
GAP_FACTOR = 2.0

# metric key -> (column in resilience_metrics.csv, colour-bar label, cell format, change)
# change: "abs" = cell minus same-year base, "pct" = (cell / same-year base - 1) * 100.
# Keep the keys in sync with HEATMAP_METRICS in rules/plot_disruption.smk.
METRICS = {
    "ens_power": ("ens_power_GWh", "unserved electricity {label}, {window} (GWh)",
                  "{:+,.2f}", "abs"),
    "price_normal": ("price_normal_EUR_per_MWh",
                     "load-weighted electricity price {label}, {window},\n"
                     "hours below {scarcity:,.0f} €/MWh (%)", "{:+,.1f} %", "pct"),
    "shedding_hours": ("shedding_hours_power",
                       "hours with electricity load shedding in {label}, {window} (h)",
                       "{:+,.0f}", "abs"),
    "scarcity_hours_no_shedding": ("scarcity_hours_no_shedding",
                                   "hours with electricity price {label} ≥ {scarcity:,.0f} "
                                   "€/MWh\nbut no load shedding in {label}, {window} (h)",
                                   "{:+,.0f}", "abs"),
    "consumer_cost": ("consumer_cost_MEUR",
                      "consumer cost {label}, {window}, price capped at {cap:,.0f} €/MWh\n"
                      "(price x served electricity demand)", "{:+,.0f} %", "pct"),
    "consumer_cost_uncapped": ("consumer_cost_uncapped_MEUR",
                               "consumer cost {label}, {window}, model price (scarcity "
                               "hours near VoLL)\n(price x served electricity demand)",
                               "{:+,.0f} %", "pct"),
    "consumer_cost_normal": ("consumer_cost_normal_MEUR",
                             "consumer cost {label}, {window},\n"
                             "hours below {scarcity:,.0f} €/MWh (M€)", "{:+,.1f}", "abs"),
    "consumer_cost_scarcity": ("consumer_cost_scarcity_MEUR",
                               "consumer cost {label}, {window},\n"
                               "hours ≥ {scarcity:,.0f} €/MWh (M€)", "{:+,.1f}", "abs"),
    "voll_cost": ("voll_cost_MEUR",
                  "cost of unserved energy {label}, {window}" + "\n" +
                  "(load shedding x VoLL, electricity and heat, M€)", "{:+,.0f}", "abs"),
    "dispatch_cost": ("dispatch_cost_excl_shedding_MEUR",
                      "system dispatch cost excl. load shedding,\nwhole run (M€)",
                      "{:+,.0f}", "abs"),
}
NOT_SOLVED = "#d9d9d9"
# number format of the second cell line (same-year base -> case); default "{:,.0f}"
SUB_FORMAT = {"ens_power": "{:,.2f}"}
# unit of the second cell line for % metrics (base -> case); default EUR/MWh
SUB_UNIT = {"consumer_cost": "M€", "consumer_cost_uncapped": "M€"}


# ---- per-case metrics --------------------------------------------------------------
def shedding_cost(n):
    gens = n.generators.index[n.generators.carrier.isin(SHEDDING_CARRIERS)]
    if gens.empty:
        return 0.0
    cost = n.get_switchable_as_dense("Generator", "marginal_cost")[gens]
    # Unweighted, like operating_cost() in disruption_storage_msv.py (hourly snapshots).
    return float((n.generators_t.p[gens].fillna(0.0) * cost).to_numpy().sum())


def regional_voll_cost(n, region, window):
    # EUR: load shedding on the region's buses x its marginal cost (= VoLL), in the window
    gens = n.generators.index[n.generators.carrier.isin(SHEDDING_CARRIERS)
                              & n.generators.bus.map(n.buses.location).eq(region)]
    if gens.empty:
        return 0.0
    cost = n.get_switchable_as_dense("Generator", "marginal_cost")[gens]
    p = n.generators_t.p.reindex(columns=gens, fill_value=0.0).fillna(0.0)
    return float(in_window((p * cost).sum(axis=1), window).sum())


def price_bus(n, region):
    if region in n.buses.index and n.buses.at[region, "carrier"] == "AC":
        return region
    ac = n.buses.index[(n.buses.location == region) & (n.buses.carrier == "AC")]
    if ac.empty:
        raise ValueError(f"No AC bus for region {region!r}")
    return ac[0]


def operating_cost(summary_path):
    # -> (system operating cost EUR, net emissions tCO2); NaN without a summary file
    if not summary_path or not Path(summary_path).exists():
        return np.nan, np.nan
    s = pd.read_csv(summary_path).iloc[0]
    if "operating_cost_EUR" in s:
        return float(s["operating_cost_EUR"]), float(s.get("net_emissions_tCO2", np.nan))
    # Full-horizon dispatch writes only the objective (no MSV/resource terms to remove).
    logger.warning(f"{summary_path}: no operating_cost_EUR, using objective_EUR")
    return float(s["objective_EUR"]), np.nan


def in_window(series, window):
    start, end = (pd.Timestamp(t) for t in window)
    return series[(series.index >= start) & (series.index < end)]


def case_metrics(network_path, summary_path, region, shed_noise_mw=0.1,
                 window=DEFAULT_WINDOW, scarcity_price=DEFAULT_SCARCITY_PRICE,
                 price_cap=DEFAULT_PRICE_CAP):
    import pypsa

    n = pypsa.Network(network_path)
    power = missing_demand(region_balance(n, region, "power", shed_noise_mw))
    heat = missing_demand(region_balance(n, region, "heat", shed_noise_mw))
    shed_p = in_window(power.shed_MW, window)
    shed_h = in_window(heat.shed_MW, window)
    price = in_window(n.buses_t.marginal_price[price_bus(n, region)], window)
    demand = in_window(power.demand_MW, window).reindex(price.index).fillna(0.0)
    # consumers pay only for the electricity they get: demand minus load shedding
    served = (demand - shed_p.reindex(price.index).fillna(0.0)).clip(lower=0.0)
    scarce = price >= scarcity_price
    normal = ~scarce
    cost_h = price * served                       # EUR per hour, model price
    cost_capped_h = price.clip(upper=price_cap) * served

    # Is the cut-off inside an empty price gap? Count hours close to it on either side.
    near = int(((price >= scarcity_price / GAP_FACTOR)
                & (price < scarcity_price * GAP_FACTOR)).sum())
    max_normal = float(price[normal].max()) if normal.any() else np.nan
    min_scarce = float(price[scarce].min()) if scarce.any() else np.nan
    msg = (f"{network_path}: highest normal price {max_normal:,.0f}, lowest scarcity price "
           f"{min_scarce:,.0f} EUR/MWh, {near} h between {scarcity_price / GAP_FACTOR:,.0f} "
           f"and {scarcity_price * GAP_FACTOR:,.0f} EUR/MWh")
    if near:
        logger.warning(msg + " -> normal/scarcity split depends on the cut-off; check the "
                       "price distribution and plotting.heatmap.scarcity_price")
    else:
        logger.info(msg)

    cost, co2 = operating_cost(summary_path)
    shed_cost = shedding_cost(n)
    return {
        "ens_power_GWh": float(shed_p.sum() / 1e3),
        "ens_heat_GWh": float(shed_h.sum() / 1e3),
        "price_normal_EUR_per_MWh": float(cost_h[normal].sum() / served[normal].sum()),
        "scarcity_hours": int(scarce.sum()),
        # scarcity price set elsewhere: a neighbour sheds (priced in over the cables) or
        # storage carries the value of a nearby shedding hour over
        "scarcity_hours_no_shedding": int(
            (scarce & ~(shed_p.reindex(price.index).fillna(0.0) > 0)).sum()),
        "consumer_cost_normal_MEUR": float(cost_h[normal].sum() / 1e6),
        "consumer_cost_scarcity_MEUR": float(cost_h[scarce].sum() / 1e6),
        "consumer_cost_MEUR": float(cost_capped_h.sum() / 1e6),
        "consumer_cost_uncapped_MEUR": float(cost_h.sum() / 1e6),
        "hours_above_price_cap": int((price > price_cap).sum()),
        "dispatch_cost_MEUR": cost / 1e6,
        "shedding_cost_MEUR": shed_cost / 1e6,
        "voll_cost_MEUR": regional_voll_cost(n, region, window) / 1e6,
        "dispatch_cost_excl_shedding_MEUR": (cost - shed_cost) / 1e6,
        "price_load_weighted_EUR_per_MWh": float(cost_h.sum() / served.sum()),
        "price_mean_EUR_per_MWh": float(price.mean()),
        "price_median_EUR_per_MWh": float(price.median()),
        "price_max_EUR_per_MWh": float(price.max()),
        "price_max_normal_EUR_per_MWh": max_normal,
        "price_min_scarcity_EUR_per_MWh": min_scarce,
        "hours_near_scarcity_cutoff": near,
        "shedding_hours_power": int((shed_p > 0).sum()),
        "shedding_hours_heat": int((shed_h > 0).sum()),
        "peak_shedding_power_MW": float(shed_p.max()),
        "peak_shedding_heat_MW": float(shed_h.max()),
        "power_demand_TWh": float(demand.sum() / 1e6),
        "heat_demand_TWh": float(in_window(heat.demand_MW, window).sum() / 1e6),
        "net_emissions_MtCO2": co2 / 1e6,
    }


# ---- heatmaps ----------------------------------------------------------------------
def load_table(files):
    # files: {(weather_year, case): path}
    rows = {key: pd.read_csv(path, index_col=0).iloc[:, 0] for key, path in files.items()}
    table = pd.DataFrame(rows).T
    table.index.names = ["weather_year", "case"]
    return table.astype(float)


def deltas(table, years, cases, reference_year, reference_case):
    """Return {name: DataFrame (index (year, case))}: absolute, differences to the
    reference cell and to the same-year base, and % change to the same-year base."""
    full = table.reindex(pd.MultiIndex.from_product([years, cases],
                                                    names=["weather_year", "case"]))
    same_year_base = full.xs(reference_case, level="case")  # NaN where not solved
    base = same_year_base.reindex(full.index.get_level_values("weather_year")).to_numpy()
    out = {"absolute": full,
           "vs_same_year": full - base,
           "vs_same_year_pct": (full / np.where(base == 0, np.nan, base) - 1) * 100}
    if (reference_year, reference_case) in table.index:
        ref = table.loc[(reference_year, reference_case)]
        out["vs_reference"] = full - ref
        out["vs_reference_pct"] = (full / ref.where(ref != 0) - 1) * 100
    else:
        logger.warning(f"Reference cell {reference_year}/{reference_case} is not solved")
    return out


def heatmap(grid, cbar_label, fmt, mark=None, sub=None):
    # grid: DataFrame rows = case labels, columns = weather years; NaN = not solved.
    # sub: optional DataFrame of strings (same shape) printed as a smaller second line.
    values = grid.to_numpy(dtype=float)
    subs = sub.to_numpy(dtype=object) if sub is not None else None
    finite = values[np.isfinite(values)]
    lim = float(np.abs(finite).max()) if finite.size else 0.0
    lim = lim if lim > 0 else 1.0
    cmap = plt.get_cmap("RdBu_r").copy()
    cmap.set_bad(NOT_SOLVED)

    nrows, ncols = values.shape
    fig, ax = plt.subplots(figsize=(1.35 * ncols + 3.0, 0.55 * nrows + 2.4),
                           constrained_layout=True)
    im = ax.imshow(np.ma.masked_invalid(values), cmap=cmap, vmin=-lim, vmax=lim, aspect="auto")
    for i in range(nrows):
        for j in range(ncols):
            v = values[i, j]
            if not np.isfinite(v):
                ax.text(j, i, "n/s", ha="center", va="center", fontsize=9, color="#5f5f5f")
                continue
            text = fmt.format(0.0 if abs(v) < 1e-9 else v)
            if text.startswith("-") and not any(ch in "123456789" for ch in text):
                text = "+" + text[1:]  # tiny negative rounding noise shown as "-0"
            color = "white" if abs(v) > 0.6 * lim else "#1a1a1a"
            second = subs[i, j] if subs is not None and isinstance(subs[i, j], str) else None
            ax.text(j, i - 0.13 if second else i, text, ha="center", va="center",
                    fontsize=9, color=color)
            if second:
                ax.text(j, i + 0.2, second, ha="center", va="center", fontsize=7.5,
                        color=color)
    if mark is not None:
        i, j = mark
        ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False, lw=2, ec="#1a1a1a",
                                   zorder=5, clip_on=False))

    ax.set_xticks(range(ncols), grid.columns)
    ax.set_yticks(range(nrows), grid.index)
    ax.xaxis.tick_top()
    ax.set_xticks(np.arange(ncols + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(nrows + 1) - 0.5, minor=True)
    ax.grid(which="minor", color="white", lw=2)
    ax.tick_params(which="both", length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    # colour bar and its description under the heatmap
    cbar = fig.colorbar(im, ax=ax, orientation="horizontal", location="bottom",
                        shrink=0.6, aspect=35, pad=0.03)
    cbar.set_label(cbar_label, fontsize=9)
    cbar.outline.set_visible(False)
    return fig


def window_label(window):
    start, end = (pd.Timestamp(t) for t in window)
    return f"{start.day} {start:%b} – {end.day} {end:%b}"


def plot_all(files, years, cases, labels, reference_year, reference_case, region_label,
             outputs, window=DEFAULT_WINDOW, scarcity_price=DEFAULT_SCARCITY_PRICE,
             price_cap=DEFAULT_PRICE_CAP):
    table = load_table(files)
    frames = deltas(table, years, cases, reference_year, reference_case)
    rows = [c for c in cases if c != reference_case]
    fill = dict(label=region_label, window=window_label(window), scarcity=scarcity_price,
                cap=price_cap)

    near = frames["absolute"].get("hours_near_scarcity_cutoff")
    if near is not None and (near > 0).any():
        cells = ", ".join(f"{y}/{c} ({v:.0f} h)" for (y, c), v in near[near > 0].items())
        logger.warning(f"prices close to the scarcity cut-off {scarcity_price:,.0f} EUR/MWh "
                       f"in: {cells}. The normal/scarcity split depends on the cut-off.")

    absolute = frames["absolute"]
    ref_label = labels.get(reference_case, reference_case)
    # vs_same_year: outage effect alone (no-outage row left out)
    # vs_reference: weather + outage vs the reference cell (no-outage case in the reference
    #               weather year, framed); its row shows the weather effect alone
    baselines = {"vs_same_year": (rows, "vs same-year no-outage case", None)}
    if "vs_reference" in frames:
        baselines["vs_reference"] = (
            list(cases), f"vs {reference_year} {ref_label} (weather + outage)",
            (list(cases).index(reference_case), list(years).index(reference_year)))
    for baseline, (rows_b, vs_text, mark) in baselines.items():
        for key, (column, label, fmt, change) in METRICS.items():
            frame = frames[f"{baseline}_pct" if change == "pct" else baseline]
            grid = frame[column].unstack("weather_year").reindex(index=rows_b, columns=years)
            # second line in every cell: baseline value -> this cell, in the metric's unit
            level = absolute[column].unstack("weather_year").reindex(columns=years)
            if baseline == "vs_same_year":
                base = level.loc[reference_case] if reference_case in level.index else None
            else:
                ref = absolute.at[(reference_year, reference_case), column]
                base = pd.Series(ref, index=years)
            level_fmt = SUB_FORMAT.get(key, "{:,.0f}")
            sub = level.reindex(index=rows_b).apply(
                lambda col: [f"{level_fmt.format(base[col.name])} → {level_fmt.format(v)}"
                             if base is not None and np.isfinite(base[col.name])
                             and np.isfinite(v) else None for v in col])
            grid.index = [labels.get(c, c) for c in grid.index]
            what = "change" if change == "pct" else "Δ"
            unit = (f"\ncell: change (%), and base → case ({SUB_UNIT.get(key, '€/MWh')})" if change == "pct"
                    else "\ncell: change, and base → case")
            fig = heatmap(grid, f"{what} {label.format(**fill)}\n{vs_text}{unit}", fmt,
                          mark=mark, sub=sub)
            path = outputs.get(f"{key}_{baseline}") or (
                outputs.get(key) if baseline == "vs_same_year" else None)
            if path is None:  # not requested
                plt.close(fig)
                continue
            png = Path(path)
            png.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(png, dpi=200, bbox_inches="tight")
            plt.close(fig)

    out = pd.concat(frames, axis=1)
    out.round(4).to_csv(outputs["table"])
    logger.info("\n" + frames["absolute"][[c for c, *_ in METRICS.values()]].round(2).to_string())


if __name__ == "__main__":
    if "snakemake" not in globals():
        sys.exit(__doc__)
    logging.basicConfig(filename=snakemake.log[0], encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    opts = snakemake.params.plotting
    hm = opts.get("heatmap") or {}
    window = tuple(hm.get("window") or DEFAULT_WINDOW)
    scarcity = float(hm.get("scarcity_price", DEFAULT_SCARCITY_PRICE))
    cap = float(hm.get("price_cap", DEFAULT_PRICE_CAP))
    if snakemake.rule == "disruption_case_metrics":
        summary = snakemake.input.get("summary") or None  # optional input: [] if missing
        if isinstance(summary, (list, tuple)):
            summary = summary[0] if summary else None
        metrics = case_metrics(snakemake.input.network, summary,
                               opts.get("region", "DK0 0AC"), opts.get("shed_noise_mw", 0.1),
                               window, scarcity, cap)
        pd.Series(metrics, name="value").to_csv(snakemake.output[0], index_label="metric")
    else:
        p = snakemake.params
        files = dict(zip(map(tuple, p.cells), snakemake.input.metrics))
        outputs = {k: v for k, v in snakemake.output.items()}
        plot_all(files, list(p.weather_years), list(p.cases), opts.get("case_labels") or {},
                 p.reference_year, p.reference_case, opts.get("region_label", "DK2"), outputs,
                 window, scarcity, cap)
