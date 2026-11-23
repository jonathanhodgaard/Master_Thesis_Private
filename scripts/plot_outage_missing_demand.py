# SPDX-License-Identifier: MIT
"""Unmet demand per hour in one region (default DK2 = bus DK0 0AC), one curve per case.

Made from missing_demand_plots.ipynb. For every hour:

    unmet % = load shedding / demand * 100

Demand = everything used in the region except exports and storage charging
(electricity: fixed demand + power-to-heat + EVs + H2 + grid losses; heat: heat demand).
One figure for electricity ("power"), one for heat, as PNG (no titles: the file
name says what they show), plus
  - <stem>_summary.csv: per case and kind: unmet GWh, hours, peaks, share of annual demand
  - <stem>_hourly.csv:  hourly unmet % per kind and case

Run by rules/plot_disruption.smk, or by hand::

    python scripts/plot_outage_missing_demand.py out_dir "Base=results/a/network_solved.nc" "Øresund out=results/b/network_solved.nc"
"""
import logging
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import pypsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.outage_helpers import KIND_TITLE, missing_demand, region_balance  # noqa: E402
from scripts.plot_outage_dispatch import shift_years, weather_year_offset  # noqa: E402

logger = logging.getLogger(__name__)

KINDS = ("power", "heat")
DEFAULTS = {"region": "DK0 0AC", "region_label": "DK2", "shed_noise_mw": 0.1,
            "missing_demand_resample": None, "case_labels": {}}
# colour by the case's position in the config (Okabe-Ito, colour-blind safe); first = dark grey
PALETTE = ["#4d4d4d", "#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00", "#56B4E9",
           "#882255", "#999933", "#44AA99"]


def load(path, region, noise):
    # Load one network, keep only the hourly unmet-demand series of both kinds.
    logger.info(f"loading {path}")
    n = pypsa.Network(path)
    return {k: missing_demand(region_balance(n, region, k, noise)) for k in KINDS}


def summary(results, kind):
    rows = {}
    for name, res in results.items():
        md = res[kind]
        shed = md.shed_MW
        rows[name] = {
            "unserved GWh": shed.sum() / 1e3,
            "hours with shedding": int((shed > 0).sum()),
            "peak shedding MW": shed.max(),
            "peak unmet %": md.missing_pct.max(),
            "unmet % of annual demand": shed.sum() / md.demand_MW.sum() * 100,
            "annual demand TWh": md.demand_MW.sum() / 1e6,
        }
    return pd.DataFrame(rows).T


def plot_missing(results, kind, colors, region_label, resample=None):
    fig, ax = plt.subplots(figsize=(14, 4.8), constrained_layout=True)
    stats = summary(results, kind)
    ymax = 0.0
    for name, res in results.items():
        pct = res[kind].missing_pct
        if resample:
            pct = pct.resample(resample).max()
        s = stats.loc[name]
        if s["hours with shedding"]:
            label = (f"{name}: {s['unserved GWh']:,.1f} GWh unmet "
                     f"({s['unmet % of annual demand']:.2f} % of annual demand), "
                     f"{s['hours with shedding']:.0f} h, peak {s['peak unmet %']:.1f} %")
        else:
            label = f"{name}: no load shedding"
        ax.plot(pct.index, pct.values, drawstyle="steps-post", lw=1.2, color=colors[name], label=label)
        ymax = max(ymax, pct.max())

    first = next(iter(results.values()))[kind].index
    ax.set_xlim(first[0], first[-1])
    ax.set_ylim(0, max(ymax * 1.08, 1.0))
    loc = mdates.MonthLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:g} %"))
    ax.set_ylabel("unmet demand (% of hourly demand)" if not resample
                  else f"unmet demand ({resample} max of hourly %)")
    ax.grid(axis="y", color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.08), ncol=1, frameon=False, fontsize=9)
    return fig


def case_colors(all_cases, labels):
    # Fixed colour per case by its position in the full case list, so plotting a subset
    # never repaints a curve.
    if len(all_cases) > len(PALETTE):
        logger.warning(f"{len(all_cases)} cases but {len(PALETTE)} colours: colours repeat")
    return {labels.get(c, c): PALETTE[i % len(PALETTE)] for i, c in enumerate(all_cases)}


def main(networks, all_cases, outputs, options):
    # networks: {case: path} in plotting order; all_cases: every configured case (colours)
    labels = options.get("case_labels") or {}
    colors = case_colors(all_cases, labels)
    results = {labels.get(c, c): load(p, options["region"], options["shed_noise_mw"])
               for c, p in networks.items()}

    # x-axis labelled with the weather year (folder name); the CSVs keep the model timestamps
    first = next(iter(results.values()))["power"].index
    offset = weather_year_offset(next(iter(networks.values())), first)
    logger.info(f"x-axis years shifted by {offset} (weather year labels)")
    shown = {name: {k: shift_years(df, offset) for k, df in res.items()}
             for name, res in results.items()}

    for kind in KINDS:
        assert all(r[kind].missing_pct.max() <= 100 + 1e-6 for r in results.values()), kind
        fig = plot_missing(shown, kind, colors, options["region_label"],
                           options.get("missing_demand_resample"))
        png = Path(outputs[kind])
        png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(png, dpi=200, bbox_inches="tight")
        plt.close(fig)

    table = pd.concat({KIND_TITLE[k]: summary(results, k) for k in KINDS}, names=["kind", "case"])
    table.round(4).to_csv(outputs["summary"])
    logger.info("\n" + table.round(2).to_string())
    hourly = pd.concat({(KIND_TITLE[k], name): res[k].missing_pct
                        for k in KINDS for name, res in results.items()}, axis=1)
    hourly.columns.names = ["kind", "case"]
    hourly.round(4).to_csv(outputs["hourly"])


if __name__ == "__main__":
    if "snakemake" in globals():
        logging.basicConfig(filename=snakemake.log[0], encoding="utf-8", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        opts = {**DEFAULTS, **snakemake.params.plotting}
        nets = dict(zip(snakemake.params.cases, snakemake.input.networks))
        outs = {"power": snakemake.output.power, "heat": snakemake.output.heat,
                "summary": snakemake.output.summary, "hourly": snakemake.output.hourly}
        main(nets, snakemake.params.all_cases, outs, opts)
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        if len(sys.argv) < 3:
            sys.exit(__doc__)
        out_dir = Path(sys.argv[1])
        nets = dict(a.split("=", 1) for a in sys.argv[2:])
        stem = out_dir / "missing_demand" / f"missing_demand_{DEFAULTS['region_label']}"
        outs = {"power": f"{stem}_power.png", "heat": f"{stem}_heat.png",
                "summary": f"{stem}_summary.csv", "hourly": f"{stem}_hourly.csv"}
        main(nets, list(nets), outs, DEFAULTS)
