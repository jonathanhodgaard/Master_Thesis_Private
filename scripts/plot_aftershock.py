# SPDX-License-Identifier: MIT
"""Aftershock of an interconnector outage in DK2: does the effect stop when the cables
come back?

Standalone (not part of the Snakemake workflow). One figure per weather year, full
simulated year (July - June), one line per outage case, compared with the same weather
year's no-outage case (plotting.heatmap.reference_case):

  1. electricity price difference case - no-outage case (EUR/MWh): daily mean, then a
     7-day rolling mean. y-axis linear between -10 and 10, logarithmic beyond.
  2. electricity load shedding in DK2 (GWh per day), every case incl. the no-outage case

Shaded: the outage periods (darker = all outage cases, lighter = combined outages only).
The log prints, per case, what remains after its outage ended (largest 7-day price
difference from one week after the end, shedding hours after the end).

Reads every {output_directory}/{weather_year}/{case}/network_solved.nc that exists.
Run from the repository root (config files merged in order, as plot_disruption_spread.py)::

    pixi run python scripts/plot_aftershock.py [config.yaml ...]
    pixi run python scripts/plot_aftershock.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_w_V2G.yaml

Output: {output_directory}/plots/aftershock/aftershock_<label>_<weather year>.png and
aftershock_<label>.csv (daily values).
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
from scripts.outage_helpers import missing_demand, region_balance  # noqa: E402
from scripts.plot_disruption_heatmaps import price_bus  # noqa: E402
from scripts.plot_disruption_spread import CONFIG, load_config  # noqa: E402
from scripts.plot_outage_missing_demand import case_colors  # noqa: E402

MODEL_START_YEAR = 2013   # snapshots are always July 2013 - June 2014
LINTHRESH = 10.0          # EUR/MWh


def daily(path, region, noise):
    n = pypsa.Network(path)
    price = n.buses_t.marginal_price[price_bus(n, region)]
    shed = missing_demand(region_balance(n, region, "power", noise)).shed_MW
    return pd.DataFrame({"price": price, "shed": shed}), price.resample("D").mean(), \
        shed.resample("D").sum() / 1e3


def outage_end(cases_cfg, case):
    ends = [pd.Timestamp(o["end"]) for o in
            ((cases_cfg.get(case) or {}).get("interconnector_outages") or [])]
    return max(ends) if ends else None


def outage_spans(cases_cfg):
    return sorted({(pd.Timestamp(o["start"]), pd.Timestamp(o["end"]))
                   for v in cases_cfg.values()
                   for o in ((v or {}).get("interconnector_outages") or [])})


def plot_year(dprice, dshed, cases, labels, colors, base_case, spans, offset):
    shift = pd.DateOffset(years=offset)
    fig, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True, constrained_layout=True,
                             gridspec_kw={"height_ratios": [3, 2]})
    for start, end in spans:
        for ax in axes:
            ax.axvspan(start + shift, end + shift, color="#bdbdbd", alpha=0.18, lw=0, zorder=0)
    for case in cases:
        name = labels.get(case, case)
        if case in dprice and case != base_case:
            d = dprice[case].rolling(7, center=True, min_periods=1).mean()
            axes[0].plot(d.index + shift, d.values, color=colors[name], lw=1.3, label=name)
        if case in dshed:
            s = dshed[case]
            axes[1].plot(s.index + shift, s.values, color=colors[name],
                         lw=2.0 if case == base_case else 1.2, label=name)
    axes[0].axhline(0, color="#4d4d4d", lw=0.8)
    axes[0].set_yscale("symlog", linthresh=LINTHRESH, linscale=0.5)
    axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:+,.0f}" if y else "0"))
    axes[0].set_ylabel(f"price difference to\n{labels.get(base_case, base_case)} (€/MWh,\n"
                       "7-day mean of daily means)")
    axes[1].set_ylim(bottom=0)
    axes[1].set_ylabel("load shedding\n(GWh per day)")
    first = next(iter(dshed.values())).index
    axes[1].set_xlim(first[0] + shift, first[-1] + shift + pd.Timedelta(days=1))
    loc = mdates.MonthLocator()
    axes[1].xaxis.set_major_locator(loc)
    axes[1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    for ax in axes:
        ax.grid(axis="y", color="#e0e0e0", lw=0.6)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    axes[1].legend(loc="upper left", bbox_to_anchor=(0.0, -0.18), ncol=4, frameon=False, fontsize=9)
    axes[0].text(0.995, 0.97, "shaded: outage periods (darker = all outage cases)",
                 transform=axes[0].transAxes, ha="right", va="top", fontsize=8, color="#4d4d4d")
    return fig


def main(*config_paths):
    cfg = load_config(config_paths or [CONFIG])["disruption"]
    opts = cfg.get("plotting", {})
    region, label = opts.get("region", "DK0 0AC"), opts.get("region_label", "DK2")
    noise = opts.get("shed_noise_mw", 0.1)
    base_case = (opts.get("heatmap") or {}).get("reference_case", "base_weather")
    cases = list(cfg["cases"])
    labels = opts.get("case_labels") or {}
    colors = case_colors(cases, labels)
    spans = outage_spans(cfg["cases"])
    out_dir = Path(cfg["output_directory"])
    fig_dir = out_dir / "plots" / "aftershock"
    fig_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for year in cfg["weather_years"]:
        hourly, dmean, dshed = {}, {}, {}
        for case in cases:
            path = out_dir / year / case / "network_solved.nc"
            if path.exists():
                print(f"loading {path}")
                hourly[case], dmean[case], dshed[case] = daily(path, region, noise)
        if base_case not in hourly:
            print(f"{year}: no {base_case} solved, skipped")
            continue
        dprice = {c: dmean[c] - dmean[base_case] for c in dmean}
        for c in dmean:
            rows.append(pd.DataFrame({"weather_year": year, "case": c,
                                      "price_mean": dmean[c], "price_diff": dprice[c],
                                      "shed_GWh": dshed[c]}))
            end = outage_end(cfg["cases"], c)
            if end is None:
                continue
            after = dprice[c].rolling(7, center=True, min_periods=1).mean()
            after = after[after.index >= end + pd.Timedelta(days=7)]
            h = hourly[c][hourly[c].index >= end]
            print(f"  {year} {c}: after {end:%d %b} (model date): largest 7-day price "
                  f"difference {after.abs().max():,.1f} EUR/MWh, shedding hours "
                  f"{int((h.shed > 0).sum())}")
        offset = int(year[:4]) - MODEL_START_YEAR
        fig = plot_year(dprice, dshed, cases, labels, colors, base_case, spans, offset)
        png = fig_dir / f"aftershock_{label}_{year}.png"
        fig.savefig(png, dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {png}")
    if rows:
        pd.concat(rows).rename_axis("model_date").reset_index() \
            .to_csv(fig_dir / f"aftershock_{label}.csv", index=False, float_format="%.3f")


if __name__ == "__main__":
    main(*sys.argv[1:])
