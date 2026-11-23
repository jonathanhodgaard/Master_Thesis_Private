# SPDX-License-Identifier: MIT
"""Spread of load shedding across weather years, per interconnector disruption case.

Standalone (not part of the Snakemake workflow). Three panels, shared x-axis = the cases
in config order (base, single, pair, all), one dot per weather year, grey min-max bar:

  1. peak load shedding (MW)          = missing peak capacity
  2. energy not served (GWh)
  3. hours with load shedding

Electricity only, region = disruption.plotting.region (default DK2 = bus DK0 0AC), full
simulated year, absolute values. Same numbers as missing_demand_*_summary.csv
(region_balance -> missing_demand, hourly shedding below shed_noise_mw counts as 0).
Note: single outages last 2 months, combined outages 3 months.

Reads every {output_directory}/{weather_year}/{case}/network_solved.nc that exists.
Metrics are cached in the output CSV; a network is only reloaded when it is new or
its file changed. Run from the repository root::

    pixi run python scripts/plot_disruption_spread.py [config.yaml ...]

Several config files are merged in the given order (later files win, nested keys merged),
like Snakemake's --configfiles. 2035 example (same for wo_V2G)::

    pixi run python scripts/plot_disruption_spread.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_w_V2G.yaml
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.outage_helpers import (  # noqa: E402
    FALLBACK_COLORS, INTERCONNECTOR_NAMES, NON_SOURCE_GROUPS, SUPPLY_ORDER, USE_ORDER,
    build_colors, interconnector_groups,
    missing_demand, region_balance,
)

CONFIG = "config/config.interconnector_disruption.yaml"
# reference weather year first (black), then Okabe-Ito (colour-blind safe)
YEAR_COLORS = ["#1a1a1a", "#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00", "#56B4E9"]
PANELS = [  # column, y-axis label
    ("peak_shedding_MW", "peak load shedding\n(MW)"),
    ("ens_GWh", "energy not served\n(GWh)"),
    ("shedding_hours", "hours with\nload shedding"),
]
# x positions get a gap between the groups base | single | pair | all
GROUP_GAP = 0.6
BAND_HALF = 0.3  # half width of the grey min-max band (x units)
# supply-mix period (end exclusive): the longest outage window (combined outages).
# Snapshots carry the model year 2013-2014 in every weather year.
SUPPLY_WINDOW = ("2013-12-01", "2014-03-01")


def supply_shares(bal, window):
    """Share (%) of the region's electricity demand covered by each source in the window.

    Local generation first, hour by hour:
      - imports cover only the region's NET import of that hour (sum of all cable flows,
        + = in), capped at the demand; in hours with net export they cover nothing. Power
        that flows in on one cable and out on another (transit) is not import.
      - the net import is split over the cables that flow in that hour, in proportion to
        their inflow.
      - the rest of the hour's demand is covered by the local sources (positive part of
        every local group, incl. load shedding), in proportion to their output; in export
        hours this scales local output down by the exported part.
    Storage and V2G are no source. Summed over the window and divided by the window's
    demand, so the shares add up to 100 %.
    Demand = missing_demand (everything used except exports and storage charging).
    """
    bal = bal.loc[window[0]:window[1]]
    bal = bal[bal.index < pd.Timestamp(window[1])]  # end is exclusive
    ics = interconnector_groups(bal)
    local = bal.drop(columns=ics + NON_SOURCE_GROUPS, errors="ignore").clip(lower=0)
    demand = missing_demand(bal).demand_MW
    net_in = bal[ics].sum(axis=1).clip(lower=0).clip(upper=demand)
    inflow = bal[ics].clip(lower=0)
    imports = inflow.div(inflow.sum(axis=1).where(inflow.sum(axis=1) > 0), axis=0) \
        .mul(net_in, axis=0).fillna(0.0)
    rest = demand - net_in
    local_total = local.sum(axis=1)
    local_cov = local.div(local_total.where(local_total > 0), axis=0).mul(rest, axis=0) \
        .fillna(0.0)
    covered = pd.concat([local_cov, imports], axis=1)
    shares = covered.sum() / demand.sum() * 100
    return shares[shares > 0.005], ics


def window_energy(bal, window, colors):
    """Energy (GWh) over the window, long DataFrame (kind, group, GWh, color):
    supply = positive hours of each local group (incl. load shedding), use = negative hours
    of each local group (as a positive number), link = net flow per interconnector
    (+ = into the region), storage = net discharge of storage and V2G together."""
    bal = bal.loc[window[0]:window[1]]
    bal = bal[bal.index < pd.Timestamp(window[1])]  # end is exclusive
    ics = interconnector_groups(bal)
    local = bal.drop(columns=ics + NON_SOURCE_GROUPS, errors="ignore")
    parts = [("supply", local.clip(lower=0).sum()), ("use", -local.clip(upper=0).sum()),
             ("link", bal[ics].sum()),
             ("storage", pd.Series({"Storage": bal.reindex(columns=NON_SOURCE_GROUPS)
                                    .fillna(0.0).sum(axis=1).sum()}))]
    rows = [(kind, g, v / 1e3, colors.get(g, "")) for kind, s in parts for g, v in s.items()]
    return pd.DataFrame(rows, columns=["kind", "group", "GWh", "color"])


def cut_groups(n, region, outages):
    # interconnector groups (as named in region_balance) of the lines/links a case cuts
    groups = set()
    for o in outages or []:
        for comp, names in (("Line", o.get("lines") or []), ("Link", o.get("links") or [])):
            st = n.components[comp].static
            for name in map(str, names):
                if name not in st.index:
                    continue
                far = st.at[name, "bus1"] if st.at[name, "bus0"] == region else st.at[name, "bus0"]
                groups.add(INTERCONNECTOR_NAMES.get((region, far), f"Interconnectors {far[:2]}"))
    return sorted(groups)


def metrics(path, region, noise, window, outages=None):
    # -> (aggregate metrics, hourly shedding MW in the hours with shedding,
    #     supply shares in the window as a long DataFrame: group, share_pct, color, kind,
    #     energy per source/use/link in the window: kind, group, GWh, color; plus one
    #     kind="cut" row per interconnector group the case cuts)
    import pypsa

    n = pypsa.Network(path)
    bal = region_balance(n, region, "power", noise)
    md = missing_demand(bal)
    shed = md.shed_MW
    agg = {"peak_shedding_MW": float(shed.max()), "ens_GWh": float(shed.sum() / 1e3),
           "shedding_hours": int((shed > 0).sum())}
    shares, ics = supply_shares(bal, window)
    colors = build_colors(n)
    mix = pd.DataFrame({"group": shares.index, "share_pct": shares.to_numpy(),
                        "color": [colors.get(g, "") for g in shares.index],
                        "kind": ["import" if g in ics else "local" for g in shares.index],
                        "method": MIX_METHOD})
    energy = window_energy(bal, window, colors)
    cut = pd.DataFrame({"kind": "cut", "group": cut_groups(n, region, outages), "GWh": 0.0,
                        "color": ""})
    return (agg, md.loc[shed > 0, ["shed_MW", "demand_MW"]], mix,
            pd.concat([energy, cut], ignore_index=True))


HOURLY_COLS = ["weather_year", "case", "snapshot", "shed_MW", "demand_MW"]
MIX_COLS = ["weather_year", "case", "group", "share_pct", "color", "kind", "method",
            "network_mtime"]
MIX_METHOD = "local_first"  # cached shares made with another method are recomputed
ENERGY_COLS = ["weather_year", "case", "kind", "group", "GWh", "color", "network_mtime"]


def _read(csv, cols):
    return (pd.read_csv(csv, dtype={"weather_year": str}) if csv.exists()
            else pd.DataFrame(columns=cols))


def collect(out_dir, years, cases, region, noise, cache_csv, hourly_csv, mix_csv, energy_csv,
            window, outages=None):
    # outages: {case: interconnector_outages list from the config} (cut links per case)
    outages = outages or {}
    # -> (table: one row per solved cell, hourly: one row per hour with shedding,
    #     mix: supply shares per cell and group, energy: GWh per cell, kind and group)
    cache = (pd.read_csv(cache_csv, dtype={"weather_year": str}).set_index(["weather_year", "case"])
             if cache_csv.exists() else pd.DataFrame())
    hourly_cache = _read(hourly_csv, HOURLY_COLS)
    mix_cache = _read(mix_csv, MIX_COLS)
    energy_cache = _read(energy_csv, ENERGY_COLS)
    rows, hourly, mixes, energies = [], [], [], []
    for year in years:
        for case in cases:
            path = out_dir / year / case / "network_solved.nc"
            if not path.exists():
                continue
            mtime = path.stat().st_mtime
            cached_h = hourly_cache[(hourly_cache.weather_year == year)
                                    & (hourly_cache.case == case)]
            cached_m = mix_cache[(mix_cache.weather_year == year) & (mix_cache.case == case)]
            cached_e = energy_cache[(energy_cache.weather_year == year)
                                    & (energy_cache.case == case)]
            if ((year, case) in cache.index and cache.at[(year, case), "network_mtime"] == mtime
                    and len(cached_h) == cache.at[(year, case), "shedding_hours"]
                    and "demand_MW" in hourly_cache and cached_h.demand_MW.notna().all()
                    and len(cached_m) and (cached_m.network_mtime == mtime).all()
                    and "method" in cached_m and (cached_m["method"] == MIX_METHOD).all()
                    and len(cached_e) and (cached_e.network_mtime == mtime).all()
                    and ((cached_e.kind == "cut").any() or not outages.get(case))):
                row, h, m, e = cache.loc[(year, case)].to_dict(), cached_h, cached_m, cached_e
            else:
                print(f"loading {path}")
                agg, shed, m, e = metrics(path, region, noise, window, outages.get(case))
                row = {**agg, "network_mtime": mtime}
                h = pd.DataFrame({"weather_year": year, "case": case,
                                  "snapshot": shed.index.astype(str),
                                  "shed_MW": shed.shed_MW.to_numpy(),
                                  "demand_MW": shed.demand_MW.to_numpy()})
                m = m.assign(weather_year=year, case=case, network_mtime=mtime)
                e = e.assign(weather_year=year, case=case, network_mtime=mtime)
            rows.append({"weather_year": year, "case": case, **row})
            hourly.append(h)
            mixes.append(m[MIX_COLS])
            energies.append(e[ENERGY_COLS])
    cols = ["weather_year", "case"] + [c for c, _ in PANELS] + ["network_mtime"]
    hourly = pd.concat(hourly, ignore_index=True) if hourly else pd.DataFrame(columns=HOURLY_COLS)
    mix = pd.concat(mixes, ignore_index=True) if mixes else pd.DataFrame(columns=MIX_COLS)
    energy = (pd.concat(energies, ignore_index=True) if energies
              else pd.DataFrame(columns=ENERGY_COLS))
    return pd.DataFrame(rows, columns=cols), hourly[HOURLY_COLS], mix, energy


def x_positions(cases):
    # groups by number of outages: base (0), singles (1), pairs (2), all (3)
    def group(case):
        if case.startswith("base"):
            return 0
        if case == "eastern_denmark_all":
            return 3
        return 1 + case.replace("_interconnector", "").count("_")
    pos, x, last = {}, 0.0, None
    for c in cases:
        g = group(c)
        if last is not None and g != last:
            x += GROUP_GAP
        pos[c], x, last = x, x + 1, g
    return pos


def plot(table, years, cases, labels):
    pos = x_positions(cases)
    colors = {y: YEAR_COLORS[i % len(YEAR_COLORS)] for i, y in enumerate(years)}
    # dots side by side in chronological order (inside the band), reference marked by size
    order = sorted(years)
    offset = {y: (order.index(y) - (len(years) - 1) / 2) * 0.12 for y in years}

    fig, axes = plt.subplots(len(PANELS), 1, figsize=(10, 8.5), sharex=True,
                             constrained_layout=True)
    for ax, (col, ylabel) in zip(axes, PANELS):
        for case in cases:
            vals = table.loc[table.case == case, col]
            if len(vals) > 1:  # min-max band behind all dots of the case
                ax.add_patch(plt.Rectangle((pos[case] - BAND_HALF, vals.min()), 2 * BAND_HALF,
                                           vals.max() - vals.min(), color="#e0e0e0", lw=0,
                                           zorder=1))
        for i, year in enumerate(years):
            sub = table[table.weather_year == year]
            if sub.empty:
                continue
            x = [pos[c] + offset[year] for c in sub.case]
            ref = i == 0
            ax.scatter(x, sub[col], s=55 if ref else 38, color=colors[year], zorder=3,
                       clip_on=False,
                       edgecolor="white" if not ref else "#1a1a1a", lw=0.8,
                       label=f"{year} (reference)" if ref else year)
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.margins(y=0.12)
        ax.grid(axis="y", color="#e0e0e0", lw=0.6)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        # separators between the groups
        xs = sorted(pos.values())
        for a, b in zip(xs, xs[1:]):
            if b - a > 1:
                ax.axvline((a + b) / 2, color="#9e9e9e", lw=0.8, ls=":")

    solved = table.groupby("case").size()
    ticks = [f"{labels.get(c, c).replace(' + ', ' +' + chr(10)).replace(' (', chr(10) + '(')}"
             f"\n[{solved.get(c, 0)}/{len(years)} years]" for c in cases]
    axes[-1].set_xticks([pos[c] for c in cases], ticks, fontsize=8.5)
    axes[-1].set_xlim(min(pos.values()) - 0.6, max(pos.values()) + 0.6)
    handles, names = axes[0].get_legend_handles_labels()
    # legend lists all configured weather years, also those not solved yet
    shown = set(names)
    for i, y in enumerate(years):
        if y not in shown and f"{y} (reference)" not in shown:
            handles.append(plt.Line2D([], [], marker="o", ls="", color=colors[y], alpha=0.35))
            names.append(f"{y} (not solved)")
    fig.legend(handles, names, loc="outside upper center", ncol=len(years), frameon=False,
               fontsize=9, title="weather year", title_fontsize=9)
    return fig


def case_ticks(cases, labels):
    return [labels.get(c, c).replace(" + ", " +\n").replace(" (", "\n(") for c in cases]


def box_stats(hourly, table, cases, scope):
    # n, min, quartiles, p90, max of the hours with shedding, per case
    rows = []
    for case in cases:
        v = hourly.loc[hourly.case == case, "shed_MW"]
        q = v.quantile([0, 0.25, 0.5, 0.75, 0.9, 1]).to_numpy() if len(v) else [np.nan] * 6
        rows.append({"scope": scope, "case": case, "solved_years": int((table.case == case).sum()),
                     "n_hours": len(v), "min_MW": q[0], "p25_MW": q[1], "median_MW": q[2],
                     "p75_MW": q[3], "p90_MW": q[4], "max_MW": q[5]})
    return pd.DataFrame(rows)


def box_plot(hourly, table, cases, labels, years, colors, ymax):
    """Box per case of the missing capacity (MW) in the hours with shedding.

    Box = 25-75 %, line = median, whiskers = min-max (top = peak missing capacity).
    Dots = every hour with shedding, coloured by weather year. n = hours (row at bottom).
    """
    pos = x_positions(cases)
    rng = np.random.default_rng(0)
    fig, ax = plt.subplots(figsize=(10, 5.2), constrained_layout=True)
    solved = table.groupby("case").size()
    for case in cases:
        v = hourly.loc[hourly.case == case]
        x = pos[case]
        if len(v):
            ax.boxplot(v.shed_MW, positions=[x], widths=0.55, whis=(0, 100), showfliers=False,
                       patch_artist=True, zorder=2,
                       boxprops={"facecolor": "#f0f0f0", "edgecolor": "#4d4d4d", "lw": 1},
                       medianprops={"color": "#1a1a1a", "lw": 1.8},
                       whiskerprops={"color": "#4d4d4d"}, capprops={"color": "#4d4d4d"})
            for year in years:
                s = v.loc[v.weather_year == year, "shed_MW"]
                if len(s):
                    ax.scatter(x + rng.uniform(-0.2, 0.2, len(s)), s, s=9, alpha=0.7,
                               color=colors[year], lw=0, zorder=3)
            text = f"n = {len(v)} h"
        else:
            text = "no shedding" if solved.get(case, 0) else "not solved"
        ax.text(x, 0.98, text, transform=ax.get_xaxis_transform(), ha="center", va="top",
                fontsize=8.5, color="#1a1a1a" if len(v) else "#8a8a8a")

    xs = sorted(pos.values())
    for a, b in zip(xs, xs[1:]):
        if b - a > 1:
            ax.axvline((a + b) / 2, color="#9e9e9e", lw=0.8, ls=":")
    ax.set_xticks([pos[c] for c in cases], case_ticks(cases, labels), fontsize=8.5)
    ax.set_xlim(min(xs) - 0.6, max(xs) + 0.6)
    ax.set_ylim(0, ymax * 1.12)  # headroom for the n row
    ax.set_ylabel("missing capacity (MW)\nin hours with load shedding")
    ax.grid(axis="y", color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    shown = [y for y in years if (hourly.weather_year == y).any()]
    if len(shown) > 1:
        handles = [plt.Line2D([], [], marker="o", ls="", color=colors[y], markersize=5)
                   for y in shown]
        fig.legend(handles, shown, loc="outside upper center", ncol=len(shown), frameon=False,
                   fontsize=9, title="weather year", title_fontsize=9)
    return fig


def find_events(hourly):
    """Shedding events = runs of consecutive hours with shedding (strict: any gap ends it).

    One row per event: weather_year, case, start, duration_h, peak_MW, ens_MWh, and
    peak_pct = largest hourly share of the region's demand that is shed (% of that hour's
    demand), mean_pct = shed energy / demand energy over the event's hours.
    """
    cols = ["weather_year", "case", "start", "duration_h", "peak_MW", "ens_MWh",
            "peak_pct", "mean_pct"]
    out = []
    for (year, case), g in hourly.groupby(["weather_year", "case"], sort=False):
        g = g.assign(t=pd.to_datetime(g.snapshot),
                     pct=g.shed_MW / g.demand_MW * 100).sort_values("t")
        run = g.t.diff().ne(pd.Timedelta(hours=1)).cumsum()
        e = g.groupby(run.to_numpy()).agg(start=("t", "first"), duration_h=("shed_MW", "size"),
                                          peak_MW=("shed_MW", "max"), ens_MWh=("shed_MW", "sum"),
                                          peak_pct=("pct", "max"), demand=("demand_MW", "sum"))
        e["mean_pct"] = e.ens_MWh / e.demand * 100
        out.append(e.drop(columns="demand").assign(weather_year=year, case=case))
    return pd.concat(out, ignore_index=True)[cols] if out else pd.DataFrame(columns=cols)


def event_table(ev, households, labels):
    """Readable event table: start in the weather year's calendar, peak share of demand and
    a household equivalent = peak share x number of households in the region (as if the
    shedding were spread evenly over all consumers)."""
    def weather_date(row):
        start = pd.Timestamp(row.start)
        offset = int(str(row.weather_year)[:4]) - (start.year if start.month >= 7 else start.year - 1)
        return start.replace(year=start.year + offset)

    t = ev.copy()
    t["start_weather_year"] = [weather_date(r) for r in t.itertuples()]
    t["households_at_peak"] = t.peak_pct / 100 * households
    t["case_label"] = t.case.map(lambda c: labels.get(c, c))
    return t.sort_values(["weather_year", "case", "start"])


def event_table_plot(t, top=15):
    """The `top` worst events (by peak MW) as a table figure."""
    w = t.sort_values("peak_MW", ascending=False).head(top)
    rows = [[r.weather_year, r.case_label, f"{r.start_weather_year:%d %b %Y, %H:%M}",
             f"{r.duration_h:.0f}", f"{r.peak_MW:,.0f}", f"{r.peak_pct:.1f} %",
             f"≈ {round(r.households_at_peak, -3):,.0f}", f"{r.ens_MWh:,.0f}"]
            for r in w.itertuples()]
    head = ["Weather\nyear", "Case", "Start", "Duration\n(h)", "Peak\n(MW)",
            "Peak share of\nDK2 demand", "Household\nequivalent", "Energy not\nserved (MWh)"]
    fig, ax = plt.subplots(figsize=(11.5, 0.33 * len(rows) + 1.0), constrained_layout=True)
    ax.axis("off")
    tab = ax.table(cellText=rows, colLabels=head, loc="center", cellLoc="center",
                   colWidths=[0.09, 0.16, 0.19, 0.08, 0.08, 0.13, 0.13, 0.13])
    tab.auto_set_font_size(False)
    tab.set_fontsize(8.5)
    tab.scale(1, 1.35)
    for (i, _), cell in tab.get_celld().items():
        cell.set_edgecolor("#d0d0d0")
        if i == 0:
            cell.set_facecolor("#f0f0f0")
            cell.set_text_props(weight="bold")
            cell.set_height(cell.get_height() * 1.6)
    return fig


SIZE_REF_MWH = [100, 500, 2000]  # dot-size legend


def _dot_size(mwh):
    return 12 + np.sqrt(np.asarray(mwh, dtype=float)) * 4


def events_plot(ev, years, colors, max_dur, max_share, max_peak):
    """Two panels, all weather years pooled, x = event duration (h):
    top    = share of the case's shedding hours in events of that duration, stacked by year
    bottom = one dot per event: peak missing MW, size = energy not served (MWh)
    """
    total_h = ev.duration_h.sum()
    durations = np.arange(1, max_dur + 1)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 7), sharex=True, constrained_layout=True,
                                   gridspec_kw={"height_ratios": [1, 1.3]})
    bottom = np.zeros(len(durations))
    rng = np.random.default_rng(0)
    for year in years:
        e = ev[ev.weather_year == year]
        if e.empty:
            continue
        hours = e.groupby("duration_h").duration_h.sum().reindex(durations, fill_value=0)
        share = hours.to_numpy() / total_h * 100
        ax1.bar(durations, share, bottom=bottom, width=0.75, color=colors[year], label=year,
                edgecolor="white", lw=0.5)
        bottom += share
        ax2.scatter(e.duration_h + rng.uniform(-0.18, 0.18, len(e)), e.peak_MW,
                    s=_dot_size(e.ens_MWh), color=colors[year], alpha=0.75,
                    edgecolor="white", lw=0.6, zorder=3)
    ax1.text(0.99, 0.95, f"{len(ev)} events, {int(total_h)} h of shedding\n"
             f"{ev.weather_year.nunique()} weather year(s) pooled",
             transform=ax1.transAxes, ha="right", va="top", fontsize=9)
    ax1.set_ylabel("share of shedding hours\n(%)")
    ax1.set_ylim(0, max_share * 1.1)
    ax2.set_ylabel("peak missing capacity\nin the event (MW)")
    ax2.set_ylim(0, max_peak * 1.1)
    ax2.set_xlabel("event duration (consecutive hours with load shedding)")
    ax2.set_xticks(durations)
    ax2.set_xlim(0.4, max_dur + 0.6)
    for ax in (ax1, ax2):
        ax.grid(axis="y", color="#e0e0e0", lw=0.6)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    year_handles, year_names = ax1.get_legend_handles_labels()
    ax1.legend(year_handles, year_names, loc="upper left", frameon=False, fontsize=9,
               title="weather year", title_fontsize=9)
    size_handles = [plt.scatter([], [], s=_dot_size(m), color="#9e9e9e", edgecolor="white")
                    for m in SIZE_REF_MWH]
    ax2.legend(size_handles, [f"{m:,} MWh" for m in SIZE_REF_MWH], loc="upper right",
               frameon=False, fontsize=9, title="energy not served", title_fontsize=9,
               labelspacing=1.2, borderpad=0.8)
    return fig


def _text_color(hex_color):
    r, g, b = (int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return "black" if 0.299 * r + 0.587 * g + 0.114 * b > 0.55 else "white"


def mix_order(mix):
    # bottom-up stack order: local sources (SUPPLY_ORDER), imports per link, Other, shedding
    groups = list(dict.fromkeys(mix.group))
    imports = [g for g in dict.fromkeys(mix.loc[mix.kind == "import", "group"])]
    out = []
    for g in SUPPLY_ORDER:
        out += imports if g == "<interconnectors>" else [g] * (g in groups)
    return [g for g in groups if g not in out and g not in ("Other", "Load shedding")] + out


def mix_colors(mix, order):
    colors = mix.dropna(subset=["color"]).drop_duplicates("group").set_index("group").color
    extra = iter(FALLBACK_COLORS * 3)
    return {g: colors.get(g) or next(extra) for g in order}


def mix_plot(mix, table, cases, labels, order, colors, min_inside=4.0):
    """100 % stacked column per case: share of electricity demand covered by each source."""
    pos = x_positions(cases)
    fig, ax = plt.subplots(figsize=(12.5, 6), constrained_layout=True)
    for case in cases:
        m = mix[mix.case == case].set_index("group").share_pct
        x = pos[case]
        if m.empty:
            ax.text(x, 50, "not solved", ha="center", va="center", rotation=90, fontsize=9,
                    color="#8a8a8a")
            continue
        bottom = 0.0
        for g in order:
            v = float(m.get(g, 0.0))
            if v <= 0:
                continue
            shed = g == "Load shedding"
            ax.bar(x, v, bottom=bottom, width=0.7, color=colors[g], lw=0.8,
                   edgecolor="#7f0000" if shed else "white", hatch="////" if shed else None,
                   label=g)
            if v >= min_inside:
                ax.text(x, bottom + v / 2, f"{v:.0f} %", ha="center", va="center", fontsize=8,
                        color=_text_color(colors[g]))
            bottom += v
        if "Load shedding" in m and m["Load shedding"] > 0:  # too thin to see: label it
            ax.text(x, 101, f"shed {m['Load shedding']:.2f} %", ha="center", va="bottom",
                    fontsize=7.5, color="#7f0000")

    xs = sorted(pos.values())
    for a, b in zip(xs, xs[1:]):
        if b - a > 1:
            ax.axvline((a + b) / 2, color="#9e9e9e", lw=0.8, ls=":")
    ax.set_xticks([pos[c] for c in cases], case_ticks(cases, labels), fontsize=8.5)
    ax.set_xlim(min(xs) - 0.6, max(xs) + 0.6)
    ax.set_ylim(0, 106)
    ax.set_yticks(range(0, 101, 20))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f} %"))
    ax.set_ylabel("share of electricity demand covered\n(1 Dec – 1 Mar)")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    handles = {lab: h for h, lab in zip(*ax.get_legend_handles_labels())}
    shown = [g for g in reversed(order) if g in handles]  # legend top-down like the stack
    ax.legend([handles[g] for g in shown], shown, loc="upper left", bbox_to_anchor=(1.01, 1.0),
              frameon=False, fontsize=8.5)
    return fig


SHED = "Load shedding"


def compensation(energy, year, case, base_case):
    """What covers the import a case loses, vs the same-year base case, over the window.

    Energy balance of the region (all in GWh, case minus base):
        lost import = more net import on the other links + more local production
                      + less own use + load shedding + storage (net discharge)
    lost import = drop of the net import (+ in, - out, over the window) on the links the
    case cuts (kind="cut" rows). A cut link that exported in the base case adds a
    negative amount (losing an export path frees power). Net flows, so transit through
    the region does not count as import. Returns (lost GWh, Series component -> GWh,
    dict component -> (color, hatched)), or None if the base case is not solved.
    """
    def cell(c):
        e = energy[(energy.weather_year == year) & (energy.case == c)]
        colors = e[e.kind != "cut"].drop_duplicates("group").set_index("group").color
        return e.set_index(["kind", "group"]).GWh, colors

    if not ((energy.weather_year == year) & (energy.case == base_case)).any():
        return None
    (c, colors), (b, _) = cell(case), cell(base_case)
    d = c.sub(b, fill_value=0.0)
    link = d.xs("link") if "link" in d.index.get_level_values(0) else pd.Series(dtype=float)
    cut = (list(c.xs("cut").index) if "cut" in c.index.get_level_values(0) else [])
    lost = float(-link.reindex(cut).fillna(0.0).sum())
    out, style = {}, {}

    def color(g, default="#9e9e9e"):
        v = colors.get(g)
        return v if isinstance(v, str) and v else default

    for g, v in link.drop(cut, errors="ignore").items():  # links still in service
        out[f"More net import: {g}"] = v
        style[f"More net import: {g}"] = (color(g), False)
    if "supply" in d.index.get_level_values(0):
        for g, v in d.xs("supply").items():
            name = SHED if g == SHED else f"More {g}"
            out[name] = v
            style[name] = ("#dd2e23", True) if g == SHED else (color(g), False)
    if "storage" in d.index.get_level_values(0):
        out["Storage (net discharge)"] = float(d.xs("storage").sum())
        style["Storage (net discharge)"] = ("#7f7f7f", False)
    if "use" in d.index.get_level_values(0):
        for g, v in d.xs("use").items():
            out[f"Less {g}"] = -v
            style[f"Less {g}"] = (color(g, "#a6a6a6"), True)
    comp = pd.Series(out, dtype=float)
    residual = lost - comp.sum()
    if abs(residual) > max(0.005 * lost, 0.5):
        comp["Balance residual"] = residual
        style["Balance residual"] = ("#ffffff", False)
    return lost, comp, style


def comp_order(names):
    # bottom-up: other links, more production (supply order), storage, less use, shedding
    def rank(name):
        if name.startswith("More net import"):
            return (0, name)
        if name.startswith("More "):
            g = name[5:]
            return (1, SUPPLY_ORDER.index(g) if g in SUPPLY_ORDER else 99, name)
        if name.startswith("Storage"):
            return (2, name)
        if name.startswith("Less "):
            g = name[5:]
            return (3, USE_ORDER.index(g) if g in USE_ORDER else 99, name)
        if name == SHED:
            return (5, name)
        return (4, name)
    return sorted(names, key=rank)


def comp_plot(results, cases, labels, solved, min_share=1.0, min_inside=4.0):
    """One bar per outage case: 100 % = the net import the case loses vs the same-year base,
    split into what covers it. Small components (< min_share % in every case) are merged;
    load shedding is always kept (and labelled above the bar), however small."""
    shares = {c: r[1] / r[0] * 100 for c, r in results.items() if r and r[0] >= 1.0}
    keep = {n for s in shares.values() for n, v in s.items()
            if abs(v) >= min_share or (n == SHED and v > 0)}
    style = {n: st for r in results.values() if r for n, st in r[2].items()}
    style["Other (each < 1 %)"] = ("#e0e0e0", False)
    merged = {}
    for c, s in shares.items():
        small = s[~s.index.isin(keep)]
        merged[c] = pd.concat([s[s.index.isin(keep)],
                               pd.Series({"Other (each < 1 %)": small.sum()})
                               if len(small) and abs(small.sum()) >= 0.05 else pd.Series(dtype=float)])
    order = comp_order({n for s in merged.values() for n in s.index})

    pos = x_positions(cases)
    fig, ax = plt.subplots(figsize=(12.5, 6.2), constrained_layout=True)
    for case in cases:
        x = pos[case]
        if case not in solved:
            ax.text(x, 50, "not solved", ha="center", va="center", rotation=90, fontsize=9,
                    color="#8a8a8a")
            continue
        if case not in merged:
            ax.text(x, 50, "no import lost", ha="center", va="center", rotation=90, fontsize=9,
                    color="#8a8a8a")
            continue
        s = merged[case]
        up, down = 0.0, 0.0
        for g in order:
            v = float(s.get(g, 0.0))
            if v == 0:
                continue
            col, hatched = style[g]
            bottom = up if v > 0 else down + v
            ax.bar(x, abs(v), bottom=bottom, width=0.7, color=col, lw=0.8,
                   edgecolor="#7f0000" if g == SHED else "white",
                   hatch="////" if g == SHED else ("xx" if hatched else None), label=g)
            if abs(v) >= min_inside:
                ax.text(x, bottom + abs(v) / 2, f"{v:.0f} %", ha="center", va="center",
                        fontsize=8, color=_text_color(col if col.startswith("#") else "#9e9e9e"))
            if v > 0:
                up += v
            else:
                down += v
        shed = float(s.get(SHED, 0.0))
        note = f"{results[case][0]:,.0f} GWh\nlost import"
        if shed > 0:
            note += f"\nshed {shed:.1f} %"
        ax.text(x, max(up, 100) + 2, note, ha="center", va="bottom", fontsize=7.5)

    xs = sorted(pos.values())
    for a, b in zip(xs, xs[1:]):
        if b - a > 1:
            ax.axvline((a + b) / 2, color="#9e9e9e", lw=0.8, ls=":")
    ax.axhline(0, color="#1a1a1a", lw=0.8)
    ax.set_xticks([pos[c] for c in cases], case_ticks(cases, labels), fontsize=8.5)
    ax.set_xlim(min(xs) - 0.6, max(xs) + 0.6)
    low = min([0.0] + [s[s < 0].sum() for s in merged.values()])
    ax.set_ylim(min(low * 1.3, -2), 125)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f} %"))
    ax.set_ylabel("share of the lost net import covered by each change\n"
                  "(vs same-year no-outage case, 1 Dec – 1 Mar)")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    handles = {lab: h for h, lab in zip(*ax.get_legend_handles_labels())}
    shown = [g for g in reversed(order) if g in handles]
    ax.legend([handles[g] for g in shown], shown, loc="upper left", bbox_to_anchor=(1.01, 1.0),
              frameon=False, fontsize=8.5, title="cross-hatched = less use", title_fontsize=8)
    return fig


def merge(base, extra):
    # nested dict merge, later file wins (as Snakemake does for --configfiles)
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(paths):
    cfg = {}
    for path in paths:
        cfg = merge(cfg, yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    return cfg


def main(*config_paths):
    cfg = load_config(config_paths or [CONFIG])["disruption"]
    opts = cfg.get("plotting", {})
    region, label = opts.get("region", "DK0 0AC"), opts.get("region_label", "DK2")
    reference = cfg.get("default_weather_year")
    years = list(cfg["weather_years"])
    years = [reference] + [y for y in years if y != reference] if reference in years else years
    cases = list(cfg["cases"])
    out_dir = Path(cfg["output_directory"])
    def folder(name):
        # one subfolder of {output_directory}/plots per plot type
        d = out_dir / "plots" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    stem = folder("spread") / f"shedding_spread_{label}"
    hourly_csv = folder("cache") / f"shedding_hourly_{label}.csv"
    mix_stem = folder("supply_mix") / f"supply_mix_{label}"
    energy_csv = folder("cache") / f"window_energy_{label}.csv"
    table, hourly, mix, energy = collect(out_dir, years, cases, region,
                                         opts.get("shed_noise_mw", 0.1), Path(f"{stem}.csv"),
                                         hourly_csv, Path(f"{mix_stem}.csv"), energy_csv,
                                         SUPPLY_WINDOW,
                                         {c: (v or {}).get("interconnector_outages")
                                          for c, v in cfg["cases"].items()})
    table.to_csv(f"{stem}.csv", index=False)
    hourly.to_csv(hourly_csv, index=False)
    mix.to_csv(f"{mix_stem}.csv", index=False)
    energy.to_csv(energy_csv, index=False)
    print(table.drop(columns="network_mtime").round(2).to_string(index=False))

    labels = opts.get("case_labels") or {}
    fig = plot(table, years, cases, labels)
    fig.savefig(f"{stem}.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {stem}.png and {stem}.csv")

    # boxplots: all years pooled + one per solved weather year, same y-scale in all
    box_stem = folder("boxplots") / f"shedding_boxplot_{label}"
    colors = {y: YEAR_COLORS[i % len(YEAR_COLORS)] for i, y in enumerate(years)}
    ymax = float(hourly.shed_MW.max()) if len(hourly) else 1.0
    scopes = [("pooled", hourly, table)] + [
        (y, hourly[hourly.weather_year == y], table[table.weather_year == y])
        for y in years if (table.weather_year == y).any()]
    stats = []
    for scope, h, t in scopes:
        fig = box_plot(h, t, cases, labels, years, colors, ymax)
        fig.savefig(f"{box_stem}_{scope}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
        stats.append(box_stats(h, t, cases, scope))
    stats = pd.concat(stats, ignore_index=True)
    stats.round(2).to_csv(f"{box_stem}.csv", index=False)
    print(stats[stats.n_hours > 0].drop(columns="solved_years").round(1).to_string(index=False))
    print(f"wrote {box_stem}_*.png and {box_stem}.csv")

    # consecutive-hour shedding events, all weather years pooled, one figure per case
    # with shedding (cases without shedding are skipped); same axes in all figures
    # supply mix: one 100 % stacked figure per solved weather year, same order and colours
    order = mix_order(mix)
    mcolors = mix_colors(mix, order)
    for year in years:
        m = mix[mix.weather_year == year]
        if m.empty:
            continue
        fig = mix_plot(m, table[table.weather_year == year], cases, labels, order, mcolors)
        fig.savefig(f"{mix_stem}_{year}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
    wide = mix.pivot_table(index="group", columns=["weather_year", "case"], values="share_pct")
    print(wide.reindex(order[::-1]).round(1).to_string())
    print(f"wrote {mix_stem}_*.png and {mix_stem}.csv")

    # compensation: what covers the lost net import, vs the same-year no-outage case
    base_case = (opts.get("heatmap") or {}).get("reference_case", "base_weather")
    outage_cases = [c for c in cases if c != base_case]
    comp_stem = folder("compensation") / f"compensation_{label}"
    rows = []
    for year in years:
        solved = set(table.loc[table.weather_year == year, "case"])
        if base_case not in solved:
            continue
        results = {c: compensation(energy, year, c, base_case)
                   for c in outage_cases if c in solved}
        for c, r in results.items():
            if r:
                rows += [{"weather_year": year, "case": c, "component": n, "GWh": v,
                          "share_pct": v / r[0] * 100 if r[0] >= 1 else np.nan,
                          "lost_import_GWh": r[0]} for n, v in r[1].items()]
        fig = comp_plot(results, outage_cases, labels, solved)
        fig.savefig(f"{comp_stem}_{year}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
    comp = pd.DataFrame(rows)
    comp.round(3).to_csv(f"{comp_stem}.csv", index=False)
    if len(comp):
        print(comp[comp.share_pct.abs() >= 0.5].round(1).to_string(index=False))
    print(f"wrote {comp_stem}_*.png and {comp_stem}.csv")

    ev_stem = folder("events") / f"shedding_events_{label}"
    ev = find_events(hourly)
    ev.round(2).to_csv(f"{ev_stem}.csv", index=False)
    if ev.empty:
        print("no shedding events")
        return

    # event table: every event with peak share of demand and household equivalent
    households = float(opts.get("households", 1_300_000))
    table_stem = folder("event_table") / f"shedding_event_table_{label}"
    et = event_table(ev, households, labels)
    et[["weather_year", "case", "start", "start_weather_year", "duration_h", "peak_MW",
        "peak_pct", "mean_pct", "households_at_peak", "ens_MWh"]].round(2) \
        .to_csv(f"{table_stem}.csv", index=False)
    fig = event_table_plot(et)
    fig.savefig(f"{table_stem}.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"household equivalent uses {households:,.0f} households in {label}")
    print(f"wrote {table_stem}.png and {table_stem}.csv")
    share = ev.groupby(["case", "duration_h"]).duration_h.sum() / ev.groupby("case").duration_h.sum()
    for case in [c for c in cases if (ev.case == c).any()]:
        fig = events_plot(ev[ev.case == case], years, colors, int(ev.duration_h.max()),
                          float(share.max() * 100), float(ev.peak_MW.max()))
        fig.savefig(f"{ev_stem}_{case}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
    print(ev.groupby(["case", "duration_h"]).size().unstack(fill_value=0).to_string())
    print(f"wrote {ev_stem}_*.png and {ev_stem}.csv")


if __name__ == "__main__":
    main(*sys.argv[1:])
