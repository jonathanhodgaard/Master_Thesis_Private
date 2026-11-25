# SPDX-License-Identifier: MIT
"""Electricity price duration curves for DK2 in the outage window.

Standalone (not part of the Snakemake workflow). The hourly electricity price of the
region (plotting.region, default DK2 = bus DK0 0AC) in one fixed window for every case
(plotting.heatmap.window, default 1 Dec - 1 Mar = the longest outage), sorted from the
highest to the lowest hour. Left part = scarcity hours (how many, how high), flat part =
the normal price level.

  price_duration_<label>_<weather year>.png   outage effect: one line per case
  price_duration_<label>_weather.png          weather effect: no-outage case only,
                                              one line per weather year
  price_duration_<label>.csv                  sorted prices of every solved cell
  ../price_boxplot/price_boxplot_<label>_<weather year>.png
                                              same prices as boxplots, one box per case
                                              (box 25-75 %, whiskers 1.5 x box, dots =
                                              hours beyond, mostly scarcity hours)

y-axis: linear from 0 to 10 EUR/MWh, logarithmic above (2035 has many hours at 0).

Reads every {output_directory}/{weather_year}/{case}/network_solved.nc that exists.
Run from the repository root (config files merged in order, as plot_disruption_spread.py)::

    pixi run python scripts/plot_price_duration.py [config.yaml ...]
    pixi run python scripts/plot_price_duration.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_w_V2G.yaml

Output: {output_directory}/plots/price_duration/
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.plot_disruption_heatmaps import DEFAULT_WINDOW, in_window, price_bus, window_label  # noqa: E402
from scripts.plot_disruption_spread import CONFIG, YEAR_COLORS, load_config  # noqa: E402
from scripts.plot_outage_missing_demand import case_colors  # noqa: E402

LINTHRESH = 10.0   # EUR/MWh: linear below, logarithmic above


def sorted_prices(path, region, window):
    n = pypsa.Network(path)
    price = in_window(n.buses_t.marginal_price[price_bus(n, region)], window)
    return np.sort(price.to_numpy())[::-1]


def style(ax, n_hours, label, window):
    ax.set_yscale("symlog", linthresh=LINTHRESH, linscale=0.4)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:,.0f}"))
    ax.set_xlim(0, n_hours)
    ax.set_xlabel(f"hours {window_label(window)}, sorted from highest to lowest price")
    ax.set_ylabel(f"electricity price {label} (€/MWh)")
    ax.grid(color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)


def plot_lines(curves, colors, widths, label, window):
    fig, ax = plt.subplots(figsize=(11, 5.5), constrained_layout=True)
    for name, p in curves.items():
        ax.plot(np.arange(len(p)), p, color=colors[name], lw=widths.get(name, 1.3), label=name)
    style(ax, max(len(p) for p in curves.values()), label, window)
    ax.legend(loc="upper right", frameon=False, fontsize=9)
    return fig


def plot_box(data, colors, label, window):
    # one box per case: box = 25-75 %, line = median, whiskers = 1.5 x box height
    # (Tukey), dots = hours beyond (mostly the scarcity hours)
    names = list(data)
    fig, ax = plt.subplots(figsize=(1.3 * len(names) + 3, 5.5), constrained_layout=True)
    bp = ax.boxplot([data[k] for k in names], patch_artist=True, widths=0.6,
                    medianprops=dict(color="#1a1a1a", lw=1.5),
                    flierprops=dict(marker="o", ms=2.5, alpha=0.4, mec="none", mfc="#4d4d4d"))
    for patch, name in zip(bp["boxes"], names):
        patch.set_facecolor(colors[name])
        patch.set_alpha(0.75)
    ax.set_xticks(range(1, len(names) + 1), names, rotation=30, ha="right")
    ax.set_yscale("symlog", linthresh=LINTHRESH, linscale=0.4)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:,.0f}"))
    ax.set_ylabel(f"electricity price {label}, {window_label(window)} (€/MWh)")
    ax.grid(axis="y", color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    return fig


def main(*config_paths):
    cfg = load_config(config_paths or [CONFIG])["disruption"]
    opts = cfg.get("plotting", {})
    region, label = opts.get("region", "DK0 0AC"), opts.get("region_label", "DK2")
    window = tuple((opts.get("heatmap") or {}).get("window") or DEFAULT_WINDOW)
    base_case = (opts.get("heatmap") or {}).get("reference_case", "base_weather")
    reference = cfg.get("default_weather_year")
    years = list(cfg["weather_years"])
    years = [reference] + [y for y in years if y != reference] if reference in years else years
    cases = list(cfg["cases"])
    labels = opts.get("case_labels") or {}
    ccolors = case_colors(cases, labels)
    out_dir = Path(cfg["output_directory"])
    fig_dir = out_dir / "plots" / "price_duration"
    fig_dir.mkdir(parents=True, exist_ok=True)

    prices = {}
    for year in years:
        for case in cases:
            path = out_dir / year / case / "network_solved.nc"
            if path.exists():
                print(f"loading {path}")
                prices[(year, case)] = sorted_prices(path, region, window)

    # outage effect: per weather year, one line per case
    for year in years:
        curves = {labels.get(c, c): prices[(year, c)] for c in cases if (year, c) in prices}
        if not curves:
            continue
        widths = {labels.get(base_case, base_case): 2.2}
        fig = plot_lines(curves, ccolors, widths, label, window)
        fig.savefig(fig_dir / f"price_duration_{label}_{year}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {fig_dir / f'price_duration_{label}_{year}.png'}")
        box_dir = out_dir / "plots" / "price_boxplot"
        box_dir.mkdir(parents=True, exist_ok=True)
        fig = plot_box(curves, ccolors, label, window)
        fig.savefig(box_dir / f"price_boxplot_{label}_{year}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {box_dir / f'price_boxplot_{label}_{year}.png'}")

    # weather effect: no-outage case, one line per weather year
    curves = {y: prices[(y, base_case)] for y in years if (y, base_case) in prices}
    if curves:
        ycolors = {y: YEAR_COLORS[i % len(YEAR_COLORS)] for i, y in enumerate(years)}
        fig = plot_lines(curves, ycolors, {reference: 2.2}, label, window)
        fig.axes[0].legend(title=labels.get(base_case, base_case), loc="upper right",
                           frameon=False, fontsize=9, title_fontsize=9)
        fig.savefig(fig_dir / f"price_duration_{label}_weather.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {fig_dir / f'price_duration_{label}_weather.png'}")

    if prices:
        table = pd.DataFrame({k: pd.Series(v) for k, v in prices.items()})
        table.columns = pd.MultiIndex.from_tuples(table.columns, names=["weather_year", "case"])
        table.index.name = "rank_hour"
        table.round(2).to_csv(fig_dir / f"price_duration_{label}.csv")


if __name__ == "__main__":
    main(*sys.argv[1:])
