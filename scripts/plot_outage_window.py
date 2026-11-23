# SPDX-License-Identifier: MIT
"""How one region (default DK2 = bus DK0 0AC) copes during an interconnector outage.

One figure per outage case, hourly, outage window +/- pad_days, outage shaded grey.
The same-year no-outage case is drawn thin grey, the outage case in its case colour
(same palette as the missing-demand plots):

  1. electricity price of the region (EUR/MWh, log axis: shedding hours reach VoLL)
  2. net import per interconnector (MW, > 0 = into the region); outage solid, base thin
  3. thermal storage state of charge (GWh): water pits solid, water tanks dashed
  4. load shedding (MW): electricity filled; heat line only if any heat is shed

The outage window is read from the network: hours where any line or link touching the
region has s_max_pu / p_max_pu = 0. No title: the file name says what it shows.

Run by rules/plot_disruption.smk, or by hand::

    python scripts/plot_outage_window.py <case>/network_solved.nc <base>/network_solved.nc out.png
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
from scripts.outage_helpers import (  # noqa: E402
    IC_COLORS, build_colors, interconnector_groups, missing_demand, region_balance,
)
from scripts.plot_outage_missing_demand import PALETTE  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULTS = {"region": "DK0 0AC", "region_label": "DK2", "shed_noise_mw": 0.1,
            "case_labels": {}, "outage_window_pad_days": 7}
BASE_COLOR = "#9e9e9e"
OUTAGE_SHADE = "#ececec"
# thermal storage groups: label -> regex on store carrier (region's heat stores)
THERMAL_STORES = {"water pits": r"water pits", "water tanks": r"water tanks"}


# ---- data ------------------------------------------------------------------------
def outage_window(n, region):
    # (start, end) of the hours where a branch touching the region is switched off.
    hours = []
    for c, attr in (("Line", "s_max_pu"), ("Link", "p_max_pu")):
        comp = n.components[c]
        st = comp.static
        touching = st.index[(st.bus0 == region) | (st.bus1 == region)]
        pu = comp.dynamic[attr].reindex(columns=touching).dropna(axis=1, how="all")
        if not pu.empty:
            off = pu.le(1e-9).any(axis=1)
            hours.extend(off.index[off])
    if not hours:
        return None
    return min(hours), max(hours)


def price_bus(n, region):
    if region in n.buses.index:
        return region
    return n.buses.index[(n.buses.location == region) & (n.buses.carrier == "AC")][0]


def thermal_soc(n, region):
    # Hourly state of charge (GWh) per thermal storage group in the region.
    st = n.stores[n.stores.bus.map(n.buses.location) == region]
    e = n.stores_t.e
    return pd.DataFrame({
        label: e.reindex(columns=st.index[st.carrier.str.contains(pat)], fill_value=0.0)
               .sum(axis=1) / 1e3
        for label, pat in THERMAL_STORES.items()
    })


def case_data(path, region, noise):
    logger.info(f"loading {path}")
    n = pypsa.Network(path)
    power = region_balance(n, region, "power", noise)
    heat = region_balance(n, region, "heat", noise)
    return {
        "n": n,
        "price": n.buses_t.marginal_price[price_bus(n, region)],
        "imports": power[interconnector_groups(power)],
        "soc": thermal_soc(n, region),
        "shed_power": missing_demand(power).shed_MW,
        "shed_heat": missing_demand(heat).shed_MW,
    }


# ---- figure ----------------------------------------------------------------------
def _style(ax, ylabel):
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False, fontsize=8.5)


def plot_window(case, base, case_label, case_color, window, pad_days, ic_colors):
    if window is None:
        logger.warning("no outage hours found: plotting the whole run without shading")
        start, end = case["price"].index[[0, -1]]
    else:
        start, end = window
    pad = pd.Timedelta(days=pad_days)
    sl = slice(start - pad, end + pad)

    fig, axes = plt.subplots(4, 1, figsize=(11, 10), sharex=True,
                             gridspec_kw={"height_ratios": [1, 1.2, 1, 1]},
                             constrained_layout=True)
    ax_price, ax_ic, ax_soc, ax_shed = axes
    thin = {"color": BASE_COLOR, "lw": 0.7}
    bold = {"color": case_color, "lw": 0.9}

    # 1. price
    ax_price.plot(base["price"][sl], label="Base (no outage)", **thin)
    ax_price.plot(case["price"][sl], label=case_label, **bold)
    ax_price.set_yscale("log")
    _style(ax_price, "electricity price\n(€/MWh, log)")

    # 2. interconnector net imports
    groups = list(dict.fromkeys(list(case["imports"]) + list(base["imports"])))
    for g in groups:
        col = ic_colors[g]
        if g in base["imports"]:
            ax_ic.plot(base["imports"][g][sl], color=col, lw=0.6, alpha=0.35)
        if g in case["imports"]:
            ax_ic.plot(case["imports"][g][sl], color=col, lw=0.9, label=g)
    ax_ic.axhline(0, color="#1a1a1a", lw=0.6)
    ax_ic.plot([], [], color="#5f5f5f", lw=0.6, alpha=0.35, label="(pale: base)")
    _style(ax_ic, "net import (MW)\n> 0 = into region")

    # 3. thermal storage state of charge
    for (label, ls) in zip(THERMAL_STORES, ("-", "--")):
        ax_soc.plot(base["soc"][label][sl], ls=ls, **thin)
        ax_soc.plot(case["soc"][label][sl], ls=ls, label=label, **bold)
    _style(ax_soc, "thermal storage\nstate of charge (GWh)")

    # 4. load shedding
    p = case["shed_power"][sl]
    ax_shed.fill_between(p.index, 0, p.to_numpy(), step="mid", color=case_color, alpha=0.8,
                         lw=0, label=f"electricity, {case_label}")
    if base["shed_power"][sl].gt(0).any():
        ax_shed.plot(base["shed_power"][sl], drawstyle="steps-mid",
                     label="electricity, base", **thin)
    for d, name, kw in ((case, case_label, bold), (base, "base", thin)):
        if d["shed_heat"][sl].gt(0).any():
            ax_shed.plot(d["shed_heat"][sl], drawstyle="steps-mid", ls=":",
                         label=f"heat, {name}", **kw)
    ax_shed.set_ylim(bottom=0)
    _style(ax_shed, "load shedding (MW)")

    for ax in axes:
        if window is not None:
            ax.axvspan(start, end + pd.Timedelta(hours=1), color=OUTAGE_SHADE, zorder=0, lw=0)
        ax.set_xlim(sl.start, sl.stop)
    # month-day only: snapshots carry the model year, not the weather year
    ax_shed.xaxis.set_major_locator(mdates.MonthLocator(bymonthday=[1, 15]))
    ax_shed.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    return fig


def main(case_path, base_path, output, case, all_cases, options):
    region = options["region"]
    labels = options.get("case_labels") or {}
    color = PALETTE[all_cases.index(case) % len(PALETTE)] if case in all_cases else PALETTE[1]
    data = case_data(case_path, region, options["shed_noise_mw"])
    base = case_data(base_path, region, options["shed_noise_mw"])

    window = outage_window(data["n"], region)
    logger.info(f"outage window: {window}")
    colors = build_colors(data["n"])
    groups = list(dict.fromkeys(list(data["imports"]) + list(base["imports"])))
    spare = iter(IC_COLORS)
    ic_colors = {g: colors.get(g) or next(spare) for g in groups}

    fig = plot_window(data, base, labels.get(case, case), color, window,
                      options.get("outage_window_pad_days", 7), ic_colors)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    if "snakemake" in globals():
        logging.basicConfig(filename=snakemake.log[0], encoding="utf-8", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        opts = {**DEFAULTS, **snakemake.params.plotting}
        main(snakemake.input.network, snakemake.input.base, snakemake.output[0],
             snakemake.wildcards.case, list(snakemake.params.all_cases), opts)
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        if len(sys.argv) != 4:
            sys.exit(__doc__)
        case_name = Path(sys.argv[1]).parent.name
        main(sys.argv[1], sys.argv[2], sys.argv[3], case_name, [], DEFAULTS)
