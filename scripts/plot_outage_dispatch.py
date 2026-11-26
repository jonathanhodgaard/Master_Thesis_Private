# SPDX-License-Identifier: MIT
"""Dispatch plots of one solved disruption case for one region (default DK2 = bus DK0 0AC).

Made from base_network_dispatch.ipynb. For electricity ("power") and heat, each as its
own figure:
  - dispatch_year:  daily-mean dispatch over the whole run (supply above 0, use below 0)
  - dispatch_zoom:  hourly dispatch in short windows (summer vs. winter)
  - demand_shares:  share of the annual demand covered by each source + net imports
  - production_shares: share of the annual local production by source (no imports)

Figures have no titles: the file name says what they show. Each figure is written as PNG.
Run by rules/plot_disruption.smk, or by hand (file names then use the network's
folder name as the case)::

    python scripts/plot_outage_dispatch.py results/disruptions/network_solved.nc out_dir
"""
import logging
import re
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
    FALLBACK_COLORS, IC_COLORS, NON_SOURCE_GROUPS, SUPPLY_ORDER, USE_ORDER,
    build_colors, interconnector_groups, is_interconnector, region_balance,
)

logger = logging.getLogger(__name__)

KINDS = ("power", "heat")
PLOTS = ("dispatch_year", "dispatch_zoom", "demand_shares", "production_shares")
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


def plot_balance(ax, bal, colors, resample="D", min_mw=1.0, note_loc="upper right"):
    # Two-sided stacked area: supply above 0, use below 0. resample=None -> hourly.
    # note_loc: "upper right" or "lower right" for the load-shedding text box.
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
        draw = df
        if resample is None:
            # Hourly values apply until the next hour, including one-hour events.
            last = df.iloc[[-1]].copy()
            last.index = pd.DatetimeIndex([df.index[-1] + pd.Timedelta(hours=1)])
            draw = pd.concat([df, last])
        kwargs = {"step": "post"} if resample is None else {}
        polys = ax.stackplot(draw.index, draw.T.values, colors=[color[g] for g in draw], linewidth=0, **kwargs)
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
    top = note_loc == "upper right"
    ax.text(0.995, 0.97 if top else 0.03, _shedding_note(shed), transform=ax.transAxes,
            ha="right", va="top" if top else "bottom",
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


def weather_year_offset(network, snapshots):
    # Years between the weather year (folder name "<start>-<end>" above the case folder,
    # e.g. .../1965-1966/<case>/network_solved.nc) and the model years of the snapshots.
    # Campaign years are July-June. January-only test runs still belong to the
    # preceding July's model weather year. Only used to label the x-axis.
    m = re.fullmatch(r"(\d{4})-\d{4}", Path(network).parent.parent.name)
    model_weather_start = snapshots[0].year - int(snapshots[0].month < 7)
    return int(m.group(1)) - model_weather_start if m else 0


def shift_years(bal, offset):
    # Same hours, dates relabelled to the weather year (months and days unchanged).
    if not offset:
        return bal
    out = bal.copy()
    out.index = bal.index.map(lambda t: t.replace(year=t.year + offset))
    return out


def plot_year(bal, label, kind, colors, resample="D"):
    fig, ax = plt.subplots(figsize=(14, 4.8), constrained_layout=True)
    supply, use = plot_balance(ax, bal, colors, resample, note_loc="lower right")
    ax.set_ylabel("GW (daily mean)" if resample == "D" else f"GW ({resample} mean)")
    ax.set_xlim(bal.index[0], bal.index[-1])
    loc = mdates.MonthLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    add_split_legend(ax, supply, use)
    return fig


def zoom_windows(index, windows):
    # All bounds use [start, end), including one-hour events.
    out = {}
    for name, (start, end) in windows.items():
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        if len(index[(index >= start) & (index < end)]):
            out[name] = (str(start), str(end))
    if not out:
        end = min(index[-1] + pd.Timedelta(hours=1), index[0] + pd.Timedelta(days=7))
        out["First week"] = (str(index[0]), str(end))
    return out


def worst_event_window(shed, index, days=14):
    # Window of `days` days around the hour with the highest load shedding
    # (7 days before, the rest after), kept inside the snapshots. None without shedding.
    if shed.max() <= 0:
        return None
    peak = shed.idxmax()
    start = (peak - pd.Timedelta(days=days // 2)).normalize()
    start = max(start, index[0].normalize())
    start = min(start, (index[-1] - pd.Timedelta(days=days - 1)).normalize())
    end = start + pd.Timedelta(days=days - 1)
    logger.info(f"worst shedding hour {peak} ({shed.max():.0f} MW): winter zoom {start:%Y-%m-%d} ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“ {end:%Y-%m-%d}")
    return f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"


def plot_zoom(bal, label, kind, colors, windows):
    # Hourly dispatch in a few short windows side by side, same y-scale.
    fig, axes = plt.subplots(1, len(windows), figsize=(14, 4.8), sharey=True,
                             constrained_layout=True, squeeze=False)
    supply, use = {}, {}
    for ax, (name, (start, end)) in zip(axes[0], windows.items()):
        sub = bal.loc[(bal.index >= pd.Timestamp(start)) & (bal.index < pd.Timestamp(end))]
        s, u = plot_balance(ax, sub, colors, resample=None)
        supply.update(s)
        use.update(u)
        ax.set_xlim(pd.Timestamp(start), pd.Timestamp(end))
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    axes[0, 0].set_ylabel("GW (hourly)")

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
    # Demand = local generation + net imports + load shedding. Storage and V2G discharge are no source.
    # Returns shares (%), demand (TWh), net flow per cable (TWh, + = import).
    ics = interconnector_groups(bal)
    cable_twh = bal[ics].sum() / 1e6
    net_imports = cable_twh.sum()
    local = bal.drop(columns=ics + NON_SOURCE_GROUPS, errors="ignore").clip(lower=0).sum() / 1e6
    local = local[local > 0]
    demand = local.sum() + net_imports

    shares = local / demand * 100
    shares = shares[(shares >= 0.01) | (shares.index == "Load shedding")]
    if net_imports > 0:
        shares["Net imports"] = net_imports / demand * 100
    return _largest_first(shares), demand, cable_twh


def _largest_first(shares):
    # largest share on the left, smallest on the right; load shedding always last
    shed = shares.loc[shares.index == "Load shedding"]
    return pd.concat([shares.drop("Load shedding", errors="ignore").sort_values(ascending=False),
                      shed])


def production_shares(bal):
    # Share (%) of the region's own annual production by source: positive (supply) hours
    # of every local group, summed over the year. Not included: imports (not produced
    # here), load shedding (not produced), storage and V2G discharge (only shift energy).
    # Returns shares (%), total production (TWh).
    drop = interconnector_groups(bal) + NON_SOURCE_GROUPS + ["Load shedding"]
    local = bal.drop(columns=drop, errors="ignore").clip(lower=0).sum() / 1e6
    local = local[local > 0]
    total = local.sum()
    shares = local / total * 100
    return _largest_first(shares[shares >= 0.01]), total


def _share_bar(shares, colors, labels=None, min_inside=4.0):
    # One horizontal 100 % bar, segments in the order of `shares`; labels: legend text
    # per group (default "<group>  <share>"). Returns fig, ax, right end of the bar.
    extra = iter(FALLBACK_COLORS)
    color = {g: NET_IMPORT_COLOR if g == "Net imports" else colors.get(g) or next(extra)
             for g in shares.index}
    fig, ax = plt.subplots(figsize=(14, 2.6), constrained_layout=True)
    left = 0.0
    for g, v in shares.items():
        kw = dict(hatch="////", edgecolor=SHED_EDGE) if g == "Load shedding" else dict(edgecolor="white")
        text = (labels or {}).get(g, f"{g}  {_pct(v)}")
        ax.barh(0, v, left=left, height=0.6, color=color[g], linewidth=1.2, label=text, **kw)
        if v >= min_inside:
            ax.text(left + v / 2, 0, f"{v:.0f} %", ha="center", va="center",
                    fontsize=9, color=_text_color(color[g]))
        left += v
    return fig, ax, left


def _finish_bar(ax, right, n_items):
    ax.set_xlim(0, max(100.0, right))
    ax.set_ylim(-0.45, 0.55)
    ax.set_yticks([])
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f} %"))
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=min(n_items, 4),
              frameon=False, fontsize=8.5)


def plot_production(bal, label, kind, colors):
    # Share of the region's own production by source (largest left), see production_shares
    shares, total = production_shares(bal)
    labels = {g: f"{g}  {_pct(v)}  ({v * total / 100:,.2f} TWh)" for g, v in shares.items()}
    fig, ax, right = _share_bar(shares, colors, labels)
    logger.info(f"[{kind}] {label} annual production {total:,.2f} TWh")
    _finish_bar(ax, right, len(shares))
    return fig


def plot_shares(bal, label, kind, colors, min_inside=4.0):
    shares, demand, cable_twh = demand_shares(bal)
    net_imports = cable_twh.sum()
    labels = {}
    if "Net imports" in shares and len(cable_twh):
        labels["Net imports"] = (f"Net imports  {_pct(shares['Net imports'])}  ("
                                 + ", ".join(f"{c.split(' (')[0]} {t:+.1f}"
                                             for c, t in cable_twh.items()) + " TWh)")
    fig, ax, left = _share_bar(shares, colors, labels, min_inside)

    # no title: demand and net-export figures go to the log
    logger.info(f"[{kind}] {label} annual demand {demand:,.2f} TWh, net imports "
                f"{net_imports:+,.2f} TWh ({net_imports / demand * 100:+.1f} % of demand)")
    if net_imports < 0 and len(cable_twh):
        ax.axvline(100, color="black", ls="--", lw=1)
        ax.text(100, 0.42, " 100 % = demand", fontsize=8.5, va="bottom")
    _finish_bar(ax, left, len(shares))
    return fig


# ---- driver -------------------------------------------------------------------------
def save(fig, png):
    # Save as PNG only.
    png = Path(png)
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=200, bbox_inches="tight")
    plt.close(fig)


def output_paths(out_dir, label, case):
    return {(plot, kind): Path(out_dir) / plot / f"{plot}_{label}_{case}_{kind}.png"
            for plot in PLOTS for kind in KINDS}


def main(network, outputs, options):
    region, label = options["region"], options["region_label"]
    n = pypsa.Network(network)
    colors = build_colors(n)
    event = n.meta.get("disruption_event")
    requested = options.get("zoom_windows", {})
    if event:
        requested = {"Selected event": [event["start"], event["end"]]}
    if not requested:
        requested = {"First week": [str(n.snapshots[0]), str(n.snapshots[min(168, len(n.snapshots)-1)])]}
    windows = zoom_windows(n.snapshots, requested)
    offset = weather_year_offset(network, n.snapshots)
    logger.info(f"x-axis years shifted by {offset} (weather year labels)")
    bals = {kind: region_balance(n, region, kind, options["shed_noise_mw"]) for kind in KINDS}
    # winter zoom: around the worst electricity shedding hour of this case (same window for
    # power and heat); without shedding the configured winter window stays
    shed = bals["power"].get("Load shedding", pd.Series(0.0, index=n.snapshots))
    worst = worst_event_window(shed, n.snapshots)
    if worst and not event:
        windows = {k: v for k, v in windows.items() if k != "Winter"}
        windows["Winter"] = worst
    for kind in KINDS:
        bal = bals[kind]
        residual = bal.sum(axis=1).abs().max()
        logger.info(f"[{kind}] max hourly balance residual: {residual:.3f} MW")
        save(plot_year(shift_years(bal, offset), label, kind, colors),
             outputs[("dispatch_year", kind)])
        save(plot_zoom(bal, label, kind, colors, windows), outputs[("dispatch_zoom", kind)])
        save(plot_shares(bal, label, kind, colors), outputs[("demand_shares", kind)])
        save(plot_production(bal, label, kind, colors), outputs[("production_shares", kind)])


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
        main(sys.argv[1], output_paths(sys.argv[2], DEFAULTS["region_label"], Path(sys.argv[1]).parent.name),
             DEFAULTS)
