# SPDX-License-Identifier: MIT
"""Dispatch plots of one solved disruption case for one region (default DK2 = bus DK0 0AC).

Made from base_network_dispatch.ipynb. For electricity ("power") and heat, each as its
own figure:
  - dispatch_year:  daily-mean dispatch over the whole run (supply above 0, use below 0)
  - dispatch_zoom:  hourly dispatch in short windows (summer vs. winter)
  - demand_shares:  share of the annual demand covered by each source + net imports

Each figure is written as PNG and PDF. Run by rules/plot_disruption.smk, or by hand::

    python scripts/plot_outage_dispatch.py results/disruptions/network_solved.nc out_dir
"""
import logging
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pypsa
from matplotlib.patches import Patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.outage_helpers import (  # noqa: E402
    FALLBACK_COLORS, IC_COLORS, KIND_TITLE, STORAGE_GROUPS, SUPPLY_ORDER, USE_ORDER,
    build_colors, interconnector_groups, is_interconnector, region_balance,
)

logger = logging.getLogger(__name__)

KINDS = ("power", "heat")
PLOTS = ("dispatch_year", "dispatch_zoom", "demand_shares")
DEFAULTS = {
    "region": "DK0 0AC",
    "region_label": "DK2",
    "shed_noise_mw": 0.1,
    "zoom_windows": {"Summer": ["2013-07-08", "2013-07-21"], "Winter": ["2014-01-13", "2014-01-26"]},
}
NET_IMPORT_COLOR = "#546e7a"
SHED_EDGE = "#7f0000"


# ---- dispatch (stacked areas) --------------------------------------------------------
def _ordered(cols, order, interconnectors):
    out = []
    for g in order:
        out += [c for c in interconnectors if c in cols] if g == "<interconnectors>" else [g] * (g in cols)
    return out + [c for c in cols if c not in out]


def _shedding_note(shed):
    shed = shed[shed > 0]
    if shed.empty:
        return "Load shedding: none"
    return f"Load shedding: {shed.sum() / 1e3:,.1f} GWh, peak {shed.max():,.0f} MW, {len(shed)} h"


def plot_balance(ax, bal, colors, resample="D", min_mw=1.0):
    # Two-sided stacked area: supply above 0, use below 0. resample=None -> hourly.
    # Returns legend handles (supply top-down, use).
    ics = interconnector_groups(bal)
    pos, neg = bal.clip(lower=0), bal.clip(upper=0)
    if resample:
        pos, neg = pos.resample(resample).mean(), neg.resample(resample).mean()
    pos, neg = pos / 1e3, neg / 1e3   # GW
    pos = pos.loc[:, pos.max() * 1e3 >= min_mw]
    neg = neg.loc[:, neg.min() * 1e3 <= -min_mw]
    pos = pos[_ordered(pos.columns, SUPPLY_ORDER, ics)]
    neg = neg[_ordered(neg.columns, USE_ORDER, ics)]

    extra, ic_extra = iter(FALLBACK_COLORS), iter(IC_COLORS)
    color = {g: colors.get(g) or next(ic_extra if g in ics else extra)
             for g in dict.fromkeys([*pos, *neg])}
    for df in (pos, neg):
        if df.empty:
            continue
        polys = ax.stackplot(df.index, df.T.values, colors=[color[g] for g in df], linewidth=0)
        for g, poly in zip(df, polys):
            if g == "Load shedding":
                poly.set_hatch("////")
                poly.set_edgecolor(SHED_EDGE)

    ax.axhline(0, color="black", lw=0.8)
    ax.grid(axis="y", color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.text(0.003, 0.97, "supply", transform=ax.transAxes, va="top", fontsize=8, color="#666")
    ax.text(0.003, 0.03, "use", transform=ax.transAxes, va="bottom", fontsize=8, color="#666")

    shed = bal.get("Load shedding", pd.Series(0.0, index=bal.index))
    ax.text(0.995, 0.97, _shedding_note(shed), transform=ax.transAxes, ha="right", va="top",
            fontsize=8.5, bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#cccccc"))
    days = shed[shed > 0].resample("D").max().dropna().index
    if resample and len(days):   # rug so short events stay visible after averaging
        ax.scatter(days, np.full(len(days), 0.0), marker="|", s=120, color="#d62728",
                   transform=ax.get_xaxis_transform(), clip_on=False, zorder=5)

    def handle(g):
        kw = dict(hatch="////", edgecolor=SHED_EDGE) if g == "Load shedding" else {}
        return Patch(facecolor=color[g], label=g, **kw)

    supply = {g: handle(g) for g in pos.columns[::-1]}
    use = {g: handle(g) for g in neg.columns if g not in supply}
    return supply, use


def add_split_legend(ax, supply, use, fontsize=8.5):
    # Supply legend from the top of the axes, use legend from just below the zero line.
    kw = dict(loc="upper left", frameon=False, fontsize=fontsize)
    y0, y1 = ax.get_ylim()
    zero = (0 - y0) / (y1 - y0)
    legends = []
    if supply:
        legends.append(ax.legend(handles=list(supply.values()), bbox_to_anchor=(1.005, 1.0), **kw))
    if use:
        legends.append(ax.legend(handles=list(use.values()), bbox_to_anchor=(1.005, zero - 0.02), **kw))
    if len(legends) == 2:
        # the layout only reserves room for the axes' registered legend -> register the wider one
        r = ax.figure.canvas.get_renderer()
        wide, narrow = sorted(legends, key=lambda l: l.get_window_extent(r).width, reverse=True)
        ax.add_artist(narrow)
        ax.legend_ = wide


def plot_year(bal, label, kind, colors, resample="D"):
    fig, ax = plt.subplots(figsize=(14, 4.8), constrained_layout=True)
    supply, use = plot_balance(ax, bal, colors, resample)
    ax.set_title(f"{label}: {KIND_TITLE[kind]} dispatch, "
                 f"{bal.index[0]:%b %Y} – {bal.index[-1]:%b %Y}",
                 loc="left", fontsize=12, fontweight="bold")
    ax.set_ylabel("GW (daily mean)" if resample == "D" else f"GW ({resample} mean)")
    ax.set_xlim(bal.index[0], bal.index[-1])
    loc = mdates.MonthLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    add_split_legend(ax, supply, use)
    return fig


def zoom_windows(index, windows):
    # Keep the configured windows that lie inside the snapshots. If none do (e.g. another
    # weather year), use two weeks from the second Monday of July and of January.
    out = {}
    for name, (start, end) in windows.items():
        if len(index[(index >= pd.Timestamp(start)) & (index < pd.Timestamp(end) + pd.Timedelta(days=1))]) > 24:
            out[name] = (str(start), str(end))
        else:
            logger.warning(f"zoom window {name} ({start} – {end}) is outside the snapshots, skipped")
    if out:
        return out
    for name, month in (("Summer", 7), ("Winter", 1)):
        days = index[(index.month == month) & (index.day >= 8)].normalize().unique()
        mondays = days[days.weekday == 0]
        if len(mondays):
            start = mondays[0]
            out[name] = (f"{start:%Y-%m-%d}", f"{start + pd.Timedelta(days=13):%Y-%m-%d}")
    if not out:   # short run: first two weeks
        start = index[0].normalize()
        out["First two weeks"] = (f"{start:%Y-%m-%d}", f"{start + pd.Timedelta(days=13):%Y-%m-%d}")
    logger.info(f"zoom windows: {out}")
    return out


def plot_zoom(bal, label, kind, colors, windows):
    # Hourly dispatch in a few short windows side by side, same y-scale.
    fig, axes = plt.subplots(1, len(windows), figsize=(14, 4.8), sharey=True,
                             constrained_layout=True, squeeze=False)
    supply, use = {}, {}
    for ax, (name, (start, end)) in zip(axes[0], windows.items()):
        sub = bal.loc[start:end]
        s, u = plot_balance(ax, sub, colors, resample=None)
        supply.update(s)
        use.update(u)
        ax.set_title(f"{name}: {sub.index[0]:%d %b %Y} – {sub.index[-1]:%d %b %Y}",
                     loc="left", fontsize=10)
        ax.set_xlim(sub.index[0], sub.index[-1])
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=2))
        ax.xaxis.set_minor_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    axes[0, 0].set_ylabel("GW (hourly)")
    fig.suptitle(f"{label}: {KIND_TITLE[kind]} dispatch, " + " vs. ".join(windows).lower(),
                 x=0.01, ha="left", fontsize=12, fontweight="bold")

    # merge the panels' legends: a group drawn above 0 in any panel counts as supply
    use = {g: h for g, h in use.items() if g not in supply}

    def ordered(handles, order):
        rank = {g: i for i, g in enumerate(order)}
        key = lambda g: rank.get("<interconnectors>" if is_interconnector(g) else g, 999)
        return {g: handles[g] for g in sorted(handles, key=key)}

    add_split_legend(axes[0, -1], ordered(supply, SUPPLY_ORDER[::-1]), ordered(use, USE_ORDER))
    return fig


# ---- demand shares (one horizontal bar) ----------------------------------------------
def _text_color(hex_color):
    r, g, b = (int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return "black" if 0.299 * r + 0.587 * g + 0.114 * b > 0.55 else "white"


def _pct(v):
    return f"{v:.1f} %" if v >= 0.1 else f"{v:.2f} %"


def demand_shares(bal):
    # Share (%) of the annual demand covered by each local source and by net imports.
    # Demand = local generation + net imports + load shedding. Storage discharge is no source.
    # Returns shares (%), demand (TWh), net flow per cable (TWh, + = import).
    ics = interconnector_groups(bal)
    cable_twh = bal[ics].sum() / 1e6
    net_imports = cable_twh.sum()
    local = bal.drop(columns=ics + STORAGE_GROUPS, errors="ignore").clip(lower=0).sum() / 1e6
    local = local[local > 0]
    demand = local.sum() + net_imports

    shares = local / demand * 100
    shares = shares[(shares >= 0.01) | (shares.index == "Load shedding")]
    shares = shares[_ordered(shares.index, SUPPLY_ORDER, [])]
    if net_imports > 0:
        shares = pd.concat([shares.drop("Load shedding", errors="ignore"),
                            pd.Series({"Net imports": net_imports / demand * 100}),
                            shares.loc[shares.index == "Load shedding"]])
    return shares, demand, cable_twh


def plot_shares(bal, label, kind, colors, min_inside=4.0):
    shares, demand, cable_twh = demand_shares(bal)
    net_imports = cable_twh.sum()
    extra = iter(FALLBACK_COLORS)
    color = {g: NET_IMPORT_COLOR if g == "Net imports" else colors.get(g) or next(extra)
             for g in shares.index}

    fig, ax = plt.subplots(figsize=(14, 2.6), constrained_layout=True)
    left = 0.0
    for g, v in shares.items():
        kw = dict(hatch="////", edgecolor=SHED_EDGE) if g == "Load shedding" else dict(edgecolor="white")
        text = f"{g}  {_pct(v)}"
        if g == "Net imports" and len(cable_twh):
            text += "  (" + ", ".join(f"{c.split(' (')[0]} {t:+.1f}" for c, t in cable_twh.items()) + " TWh)"
        ax.barh(0, v, left=left, height=0.6, color=color[g], linewidth=1.2, label=text, **kw)
        if v >= min_inside:
            ax.text(left + v / 2, 0, f"{v:.0f} %", ha="center", va="center",
                    fontsize=9, color=_text_color(color[g]))
        left += v

    title = (f"{label}: {KIND_TITLE[kind].lower()} demand by source, "
             f"{bal.index[0]:%b %Y} – {bal.index[-1]:%b %Y} (demand {demand:,.1f} TWh)")
    if net_imports < 0 and len(cable_twh):
        ax.axvline(100, color="black", ls="--", lw=1)
        ax.text(100, 0.42, " 100 % = demand", fontsize=8.5, va="bottom")
        title += f"\nnet exporter: net exports = {-net_imports / demand * 100:.1f} % of demand"
    ax.set_xlim(0, max(100.0, left))
    ax.set_ylim(-0.45, 0.55)
    ax.set_yticks([])
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f} %"))
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=min(len(shares), 4),
              frameon=False, fontsize=8.5)
    return fig


# ---- driver -------------------------------------------------------------------------
def save(fig, png):
    # Save as PNG and as PDF next to it (same name).
    png = Path(png)
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=200, bbox_inches="tight")
    fig.savefig(png.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)


def output_paths(out_dir, label):
    return {(plot, kind): Path(out_dir) / f"{plot}_{label}_{kind}.png" for plot in PLOTS for kind in KINDS}


def main(network, outputs, options):
    region, label = options["region"], options["region_label"]
    n = pypsa.Network(network)
    colors = build_colors(n)
    windows = zoom_windows(n.snapshots, options["zoom_windows"])
    for kind in KINDS:
        bal = region_balance(n, region, kind, options["shed_noise_mw"])
        residual = bal.sum(axis=1).abs().max()
        logger.info(f"[{kind}] max hourly balance residual: {residual:.3f} MW")
        save(plot_year(bal, label, kind, colors), outputs[("dispatch_year", kind)])
        save(plot_zoom(bal, label, kind, colors, windows), outputs[("dispatch_zoom", kind)])
        save(plot_shares(bal, label, kind, colors), outputs[("demand_shares", kind)])


if __name__ == "__main__":
    if "snakemake" in globals():
        logging.basicConfig(filename=snakemake.log[0], encoding="utf-8", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        opts = {**DEFAULTS, **snakemake.params.plotting}
        outs = {(plot, kind): snakemake.output[f"{plot}_{kind}"] for plot in PLOTS for kind in KINDS}
        main(snakemake.input.network, outs, opts)
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        if len(sys.argv) != 3:
            sys.exit(__doc__)
        main(sys.argv[1], output_paths(sys.argv[2], DEFAULTS["region_label"]), DEFAULTS)
