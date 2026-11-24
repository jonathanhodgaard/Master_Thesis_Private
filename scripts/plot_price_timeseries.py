# SPDX-License-Identifier: MIT
"""Daily electricity price in DK2 over the simulated year, one line per case.

Standalone (not part of the Snakemake workflow). Hourly price of the region
(plotting.region, default DK2 = bus DK0 0AC), per day:

  solid line   daily average (pulled up by scarcity hours: one hour at ~22,600 EUR/MWh
               adds ~940 EUR/MWh to that day)
  dashed line  daily median (the normal price level; ignores a few scarcity hours)
Average up but median flat = a few scarcity hours; both up = higher price all day.

  price_timeseries_<label>_<weather year>.png   outage effect: one colour per case,
                                                outage periods shaded
  price_timeseries_<label>_weather.png          weather effect: no-outage case only,
                                                one colour per weather year
  price_timeseries_<label>.csv                  daily average and median of every cell

Full year (July - June), x-axis labelled with the weather year (the snapshots are always
July 2013 - June 2014). y-axis linear from 0 to 10 EUR/MWh, logarithmic above.

Reads every {output_directory}/{weather_year}/{case}/network_solved.nc that exists.
Run from the repository root (config files merged in order, as plot_disruption_spread.py)::

    pixi run python scripts/plot_price_timeseries.py [config.yaml ...]
    pixi run python scripts/plot_price_timeseries.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_w_V2G.yaml

Output: {output_directory}/plots/price_timeseries/
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pypsa
from matplotlib.lines import Line2D

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.plot_disruption_heatmaps import price_bus  # noqa: E402
from scripts.plot_disruption_spread import CONFIG, YEAR_COLORS, load_config  # noqa: E402
from scripts.plot_outage_missing_demand import case_colors  # noqa: E402

MODEL_START_YEAR = 2013   # snapshots are always July 2013 - June 2014
LINTHRESH = 10.0          # EUR/MWh: linear below, logarithmic above


def daily_price(path, region):
    n = pypsa.Network(path)
    price = n.buses_t.marginal_price[price_bus(n, region)]
    day = price.resample("D")
    return pd.DataFrame({"mean": day.mean(), "median": day.median()})


def plot_lines(series, colors, widths, label, offset, spans=(), legend_title=None):
    # series: {name: DataFrame(mean, median)}, model dates; offset: years to add for labels
    shift = pd.DateOffset(years=offset)
    fig, ax = plt.subplots(figsize=(14, 5.5), constrained_layout=True)
    for start, end in spans:
        ax.axvspan(start + shift, end + shift, color="#bdbdbd", alpha=0.18, lw=0, zorder=0)
    for name, d in series.items():
        x = d.index + shift
        lw = widths.get(name, 1.2)
        ax.plot(x, d["mean"], color=colors[name], lw=lw, label=name)
        ax.plot(x, d["median"], color=colors[name], lw=max(lw * 0.6, 0.7), ls="--", alpha=0.9)
    first = next(iter(series.values())).index
    ax.set_xlim(first[0] + shift, first[-1] + shift + pd.Timedelta(days=1))
    loc = mdates.MonthLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.set_yscale("symlog", linthresh=LINTHRESH, linscale=0.4)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:,.0f}"))
    ax.set_ylabel(f"electricity price {label} (€/MWh)")
    ax.grid(axis="y", color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    handles, names = ax.get_legend_handles_labels()
    handles += [Line2D([], [], color="#4d4d4d", lw=1.2),
                Line2D([], [], color="#4d4d4d", lw=0.8, ls="--")]
    names += ["daily average", "daily median"]
    ax.legend(handles, names, title=legend_title, loc="upper left",
              bbox_to_anchor=(0.0, -0.1), ncol=5, frameon=False, fontsize=9, title_fontsize=9)
    if spans:
        ax.text(0.995, 0.98, "shaded: outage periods (darker = all outage cases)",
                transform=ax.transAxes, ha="right", va="top", fontsize=8, color="#4d4d4d")
    return fig


def main(*config_paths):
    cfg = load_config(config_paths or [CONFIG])["disruption"]
    opts = cfg.get("plotting", {})
    region, label = opts.get("region", "DK0 0AC"), opts.get("region_label", "DK2")
    base_case = (opts.get("heatmap") or {}).get("reference_case", "base_weather")
    reference = cfg.get("default_weather_year")
    years = list(cfg["weather_years"])
    years = [reference] + [y for y in years if y != reference] if reference in years else years
    cases = list(cfg["cases"])
    labels = opts.get("case_labels") or {}
    ccolors = case_colors(cases, labels)
    spans = sorted({(pd.Timestamp(o["start"]), pd.Timestamp(o["end"]))
                    for v in cfg["cases"].values()
                    for o in ((v or {}).get("interconnector_outages") or [])})
    out_dir = Path(cfg["output_directory"])
    fig_dir = out_dir / "plots" / "price_timeseries"
    fig_dir.mkdir(parents=True, exist_ok=True)

    data = {}
    for year in years:
        for case in cases:
            path = out_dir / year / case / "network_solved.nc"
            if path.exists():
                print(f"loading {path}")
                data[(year, case)] = daily_price(path, region)

    # outage effect: one figure per weather year, one colour per case
    for year in years:
        series = {labels.get(c, c): data[(year, c)] for c in cases if (year, c) in data}
        if not series:
            continue
        offset = int(year[:4]) - MODEL_START_YEAR
        fig = plot_lines(series, ccolors, {labels.get(base_case, base_case): 2.0}, label,
                         offset, spans)
        png = fig_dir / f"price_timeseries_{label}_{year}.png"
        fig.savefig(png, dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {png}")

    # weather effect: no-outage case, one colour per weather year (x-axis: months only,
    # since the years differ)
    series = {y: data[(y, base_case)] for y in years if (y, base_case) in data}
    if series:
        ycolors = {y: YEAR_COLORS[i % len(YEAR_COLORS)] for i, y in enumerate(years)}
        fig = plot_lines(series, ycolors, {reference: 2.0}, label, 0,
                         legend_title=labels.get(base_case, base_case))
        ax = fig.axes[0]
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
        png = fig_dir / f"price_timeseries_{label}_weather.png"
        fig.savefig(png, dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {png}")

    if data:
        table = pd.concat(data, names=["weather_year", "case", "model_date"]).reset_index()
        table.to_csv(fig_dir / f"price_timeseries_{label}.csv", index=False, float_format="%.2f")


if __name__ == "__main__":
    main(*sys.argv[1:])
