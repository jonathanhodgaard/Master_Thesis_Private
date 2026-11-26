# Legacy saved-experiment plotting; for the current campaign use rules/plot_disruption.smk
# and scripts/summarise_disruptions.py. Historical configuration versions remain in Git.
# SPDX-License-Identifier: MIT
"""Gas stored in DK2's gas store and gas used in DK2, over the year, one line per case.

Standalone (not part of the Snakemake workflow). One figure per weather year, three panels:

  1. gas in the store "DK0 0AC gas Store" (GWh, daily mean); dashed line = capacity
  2. store level minus the same weather year's no-outage case (GWh)
  3. gas used in DK2 (GWh per day): every link that takes gas from the DK2 gas bus
     (power plants, CHPs, boilers, industry, SMR), pipelines excluded

The store sits on DK2's gas bus, but that bus is connected by pipelines (to Jutland and
on to the rest of the model), so its level follows the whole gas system, not DK2 alone.
Electricity outages do not cut gas pipelines. Shaded: outage windows of the configured
cases (single outages Dec-Jan, combined outages Dec-Feb).

Reads every {output_directory}/{weather_year}/{case}/network_solved.nc that exists.
Run from the repository root (config files are merged in order, as for
plot_disruption_spread.py)::

    pixi run python scripts/plot_gas_store.py [config.yaml ...]
    pixi run python scripts/plot_gas_store.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_w_V2G.yaml

Output: {output_directory}/plots/gas_store/gas_store_<label>_<weather year>.png and
gas_store_<label>.csv (daily values of all solved cells).
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.plot_disruption_spread import CONFIG, load_config  # noqa: E402
from scripts.plot_outage_missing_demand import case_colors  # noqa: E402

MODEL_START_YEAR = 2013   # snapshots are always July 2013 - June 2014


def gas_daily(path, region):
    # daily store level (GWh, mean) and gas used in the region (GWh/day), plus capacity
    n = pypsa.Network(path)
    bus = f"{region} gas"
    store = n.stores.index[(n.stores.bus == bus) & (n.stores.carrier == "gas")]
    if store.empty:
        raise ValueError(f"no gas store on {bus} in {path}")
    level = n.stores_t.e[store].sum(axis=1) / 1e3
    capacity = float(n.stores.loc[store, "e_nom_opt"].sum()) / 1e3
    users = n.links.index[(n.links.bus0 == bus) & ~n.links.carrier.str.startswith("gas pipeline")]
    w = n.snapshot_weightings.generators
    used = n.links_t.p0.reindex(columns=users, fill_value=0.0).clip(lower=0).sum(axis=1) * w / 1e3
    daily = pd.DataFrame({"level_GWh": level.resample("D").mean(),
                          "used_GWh_per_day": used.resample("D").sum()})
    return daily, capacity


def outage_spans(cases_cfg):
    # distinct (start, end) outage windows over all cases, model calendar
    spans = {(pd.Timestamp(o["start"]), pd.Timestamp(o["end"]))
             for v in cases_cfg.values() for o in ((v or {}).get("interconnector_outages") or [])}
    return sorted(spans)


def plot_year(data, capacity, cases, labels, colors, base_case, spans, offset):
    shift = pd.DateOffset(years=offset)
    fig, axes = plt.subplots(3, 1, figsize=(13, 9), sharex=True, constrained_layout=True,
                             gridspec_kw={"height_ratios": [3, 2, 2]})
    base = data.get(base_case)
    for case in [c for c in cases if c in data]:
        d = data[case].copy()
        d.index = d.index + shift
        kw = dict(color=colors[labels.get(case, case)], lw=1.2, label=labels.get(case, case))
        if case == base_case:
            kw.update(lw=2.0)
        axes[0].plot(d.index, d.level_GWh, **kw)
        axes[2].plot(d.index, d.used_GWh_per_day, **kw)
        if base is not None and case != base_case:
            axes[1].plot(d.index, d.level_GWh - base.level_GWh.values, **kw)

    axes[0].axhline(capacity, color="#4d4d4d", ls="--", lw=1)
    axes[0].text(0.005, capacity, f" capacity {capacity:,.0f} GWh", va="bottom",
                 transform=axes[0].get_yaxis_transform(), fontsize=8, color="#4d4d4d")
    axes[0].set_ylim(0, capacity * 1.08)
    axes[1].axhline(0, color="#4d4d4d", lw=0.8)
    axes[2].set_ylim(bottom=0)
    axes[0].set_ylabel("gas in DK2 store (GWh)")
    axes[1].set_ylabel(f"store level minus\n{labels.get(base_case, base_case)} (GWh)")
    axes[2].set_ylabel("gas used in DK2\n(GWh per day)")
    if base is None:
        axes[1].text(0.5, 0.5, "no no-outage case solved for this weather year",
                     ha="center", va="center", transform=axes[1].transAxes, color="#7f7f7f")

    # shaded outage windows: lighter shade = only the longer (combined) outages
    for i, (start, end) in enumerate(spans):
        for ax in axes:
            ax.axvspan(start + shift, end + shift, color="#bdbdbd", alpha=0.18, lw=0, zorder=0)
    if spans:
        axes[0].text(spans[0][0] + shift, capacity * 1.08, " outage windows (darker = all cases)",
                     va="top", fontsize=8, color="#4d4d4d")

    first = next(iter(data.values())).index
    axes[2].set_xlim(first[0] + shift, first[-1] + shift)
    loc = mdates.MonthLocator()
    axes[2].xaxis.set_major_locator(loc)
    axes[2].xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    for ax in axes:
        ax.grid(axis="y", color="#e0e0e0", lw=0.6)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    axes[2].legend(loc="upper left", bbox_to_anchor=(0.0, -0.18), ncol=4, frameon=False, fontsize=9)
    return fig


def main(*config_paths):
    cfg = load_config(config_paths or [CONFIG])["disruption"]
    opts = cfg.get("plotting", {})
    region, label = opts.get("region", "DK0 0AC"), opts.get("region_label", "DK2")
    cases = list(cfg["cases"])
    labels = opts.get("case_labels") or {}
    colors = case_colors(cases, labels)
    base_case = (opts.get("heatmap") or {}).get("reference_case", "base_weather")
    spans = outage_spans(cfg["cases"])
    out_dir = Path(cfg["output_directory"])
    fig_dir = out_dir / "plots" / "gas_store"
    fig_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for year in cfg["weather_years"]:
        data, capacity = {}, None
        for case in cases:
            path = out_dir / year / case / "network_solved.nc"
            if not path.exists():
                continue
            print(f"loading {path}")
            data[case], capacity = gas_daily(path, region)
            rows.append(data[case].assign(weather_year=year, case=case))
        if not data:
            continue
        offset = int(year[:4]) - MODEL_START_YEAR
        fig = plot_year(data, capacity, cases, labels, colors, base_case, spans, offset)
        png = fig_dir / f"gas_store_{label}_{year}.png"
        fig.savefig(png, dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {png}")

    if rows:
        table = pd.concat(rows).rename_axis("model_date").reset_index()
        table = table[["weather_year", "case", "model_date", "level_GWh", "used_GWh_per_day"]]
        table.round(3).to_csv(fig_dir / f"gas_store_{label}.csv", index=False)
        print(f"wrote {fig_dir / f'gas_store_{label}.csv'}")


if __name__ == "__main__":
    main(*sys.argv[1:])
