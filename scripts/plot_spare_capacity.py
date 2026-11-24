# SPDX-License-Identifier: MIT
"""Spare electricity capacity in DK2: how close is each hour to load shedding?

Standalone (not part of the Snakemake workflow). For every solved case and weather year,
two figures (electricity only, MW of electricity that could be added in DK2):

  _winter  hourly, 1 November - 31 March (covers all outage windows)
  _zoom    hourly, 14 days around the worst shedding hour (without shedding: around the
           tightest hour of Dec-Feb)
The hourly CSV covers the full year.

Stacked areas, bottom up:
  local plants   capacity x availability - production, electricity output
                 (CHPs, oil, waste, biomass power, gas turbines, ...)
  storage        V2G / battery discharge, limited by power AND by the energy stored
                 above the next required minimum (EVs must be 80 % charged at a fixed hour
                 at 06:00 every day; that level plus the driving until then is reserved).
                 Recharging in between is ignored.
  wind & solar   available - produced (curtailment), hatched: not firm
Grey dashed line: stack + room on the interconnectors (import capacity - net import,
summed over the cables; 0 while a cable is out; includes stopping an export). Not stacked:
it is cable room, not available power (the neighbour may be short itself, and the model
already used every import that was worth it). Per cable in the hourly CSV.
Dark blue line: low-voltage conversion headroom = distribution-grid capacity (high -> low
voltage) - its flow. At 0 the low-voltage side (households, EVs, heat pumps) cannot get
more power even when there is spare capacity on the high-voltage side.
Below 0: load shedding (MW, hatched).

Upper bounds: fuel limits (biomass allowance, waste arrivals), CHP heat coupling, ramping and whether a
neighbour actually has power to export are ignored. The cable losses are not deducted.

Reads every {output_directory}/{weather_year}/{case}/network_solved.nc that exists.
Run from the repository root (config files merged in order, as plot_disruption_spread.py)::

    pixi run python scripts/plot_spare_capacity.py [config.yaml ...]
    pixi run python scripts/plot_spare_capacity.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_w_V2G.yaml

Output: {output_directory}/{weather_year}/{case}/plots/spare_capacity/
    spare_capacity_<label>_<case>_<weather_year>_{winter,zoom}.png and _hourly.csv
"""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.outage_helpers import (  # noqa: E402
    INTERCONNECTOR_NAMES, POWER_RULES, build_colors, missing_demand, region_balance,
)
from scripts.plot_disruption_spread import CONFIG, load_config  # noqa: E402

MODEL_START_YEAR = 2013          # snapshots are always July 2013 - June 2014
WINTER = ("2013-11-01", "2014-03-31 23:00")   # model calendar, both ends included
LV_LABEL = "Low-voltage conversion headroom"
LV_COLOR = "#0b3c8c"   # dark blue: apart from wind (light blue, hatched) and shedding (red)
SHED_COLOR, SHED_EDGE = "#dd2e23", "#8b1a14"
STORAGE_CARRIERS = r"V2G|battery discharger"
NOT_PRODUCERS = r"distribution grid|heat pump|^DC$|^AC$"
FIRM_ORDER = ["Biomass CHP", "Gas CHP", "Waste CHP", "Biomass power", "Oil", "Gas turbines",
              "Coal", "Nuclear", "Hydro", "Hydrogen", "Other"]
STORAGE_ORDER = ["EV discharging (V2G)", "Battery"]
VRES_ORDER = ["Offshore wind", "Onshore wind", "Solar"]


def group(carrier):
    for pattern, g in POWER_RULES:
        if re.search(pattern, carrier):
            return g
    return "Other"


def dense(n, c, attr):
    return n.get_switchable_as_dense(c, attr)


def free_energy(n, bus, e_min_all):
    # MWh in the stores on `bus` that could be discharged now (within one hour) without
    # missing the NEXT required minimum level (EVs: 80 % at 06:00 every day) after the
    # loads on that bus (driving) until then. Recharging in between is ignored.
    st = n.stores.index[n.stores.bus == bus]
    e = n.stores_t.e[st].sum(axis=1).values
    e_min = e_min_all[st].sum(axis=1).values
    loads = n.loads.index[n.loads.bus == bus]
    cum = n.loads_t.p.reindex(columns=loads, fill_value=0.0).sum(axis=1).cumsum().values
    pos = np.arange(len(e))
    req = np.where(e_min > 0, pos, len(e))
    nxt = np.minimum.accumulate(req[::-1])[::-1]          # next hour with a minimum level
    j = np.minimum(nxt, len(e) - 1)
    need = np.where(nxt < len(e), e_min[j] + cum[j] - cum, 0.0)
    return pd.Series(np.clip(e - need, 0.0, None), index=n.snapshots)


def spare_capacity(n, region):
    # hourly spare electricity output (MW) per group, LV headroom and load shedding
    buses = n.buses.index[(n.buses.location == region) & n.buses.carrier.isin(["AC", "low voltage"])]
    cols = {}

    def add(name, s):
        cols[name] = cols.get(name, 0.0) + s.clip(lower=0)

    # generators (wind, solar; load shedding excluded)
    gens = n.generators.index[n.generators.bus.isin(buses)
                              & ~n.generators.carrier.str.contains("load-shedding")]
    avail = dense(n, "Generator", "p_max_pu")[gens] * n.generators.loc[gens, "p_nom_opt"]
    for g in gens:
        add(group(n.generators.at[g, "carrier"]), avail[g] - n.generators_t.p[g])

    # links that deliver electricity to the region (bus1): plants and storage dischargers
    L = n.links
    into = L.index[L.bus1.isin(buses) & ~L.bus0.isin(buses)
                   & ~L.carrier.str.contains(NOT_PRODUCERS) & (L.p_nom_opt > 0)]
    pmax, eff = dense(n, "Link", "p_max_pu"), dense(n, "Link", "efficiency")
    p0 = n.links_t.p0
    e_min_all = dense(n, "Store", "e_min_pu") * n.stores.e_nom_opt
    for k in into:
        head = (L.at[k, "p_nom_opt"] * pmax[k] - p0[k]).clip(lower=0)   # input side
        if re.search(STORAGE_CARRIERS, L.at[k, "carrier"]):
            head = np.minimum(head, free_energy(n, L.at[k, "bus0"], e_min_all))
        add(group(L.at[k, "carrier"]), head * eff[k])

    # interconnectors: import capacity - net import (+ = room for more import)
    ac = region
    for (r, far), name in INTERCONNECTOR_NAMES.items():
        if r != region:
            continue
        cap, net_in = 0.0, 0.0
        lines = n.lines.index[((n.lines.bus0 == ac) & (n.lines.bus1 == far))
                              | ((n.lines.bus1 == ac) & (n.lines.bus0 == far))]
        smax = dense(n, "Line", "s_max_pu")
        for l in lines:
            cap = cap + n.lines.at[l, "s_nom_opt"] * smax[l]
            sign = -1 if n.lines.at[l, "bus0"] == ac else 1   # p0 > 0 = flow bus0 -> bus1
            net_in = net_in + sign * n.lines_t.p0[l]
        dc = L.index[(L.carrier == "DC") & (((L.bus0 == far) & (L.bus1 == ac))
                                           | ((L.bus0 == ac) & (L.bus1 == far)))]
        for k in dc:
            if L.at[k, "bus1"] == ac:   # this direction imports
                cap = cap + L.at[k, "p_nom_opt"] * pmax[k] * eff[k]
                net_in = net_in + p0[k] * eff[k]
            else:                        # this direction exports
                net_in = net_in - p0[k]
        if len(lines) or len(dc):
            cols[name] = (cap - net_in).clip(lower=0)

    spare = pd.DataFrame(cols, index=n.snapshots)
    spare = spare.loc[:, spare.max() > 1.0]   # drop groups that never have 1 MW spare

    # low-voltage conversion headroom (high -> low voltage, output side)
    dist = L.index[(L.carrier == "electricity distribution grid") & L.bus0.isin(buses)
                   & (n.buses.loc[L.bus1, "carrier"].values == "low voltage")]
    lv = sum((L.at[k, "p_nom_opt"] * pmax[k] - p0[k]).clip(lower=0) * eff[k] for k in dist)
    shed = missing_demand(region_balance(n, region, "power")).shed_MW
    return spare, pd.Series(lv, index=n.snapshots, name=LV_LABEL), shed


IC_LABEL = "+ room on interconnectors (not guaranteed)"
IC_COLOR = "#7f7f7f"


def ordered(spare):
    # stacked local groups only; interconnectors are drawn as a line (interconnector_room)
    return [c for c in FIRM_ORDER + STORAGE_ORDER + VRES_ORDER if c in spare]


def interconnector_room(spare):
    ics = [c for c in INTERCONNECTOR_NAMES.values() if c in spare]
    return spare[ics].sum(axis=1)


def firm_local(spare):
    return spare[[c for c in spare if c in FIRM_ORDER + STORAGE_ORDER]].sum(axis=1)


def zoom_window(spare, shed, days=14):
    if shed.max() > 0:
        centre = shed.idxmax()
    else:
        winter = firm_local(spare).loc["2013-12-01":"2014-02-28"]
        centre = winter.idxmin() if len(winter) else firm_local(spare).idxmin()
    start = max((centre - pd.Timedelta(days=days // 2)).normalize(), spare.index[0])
    start = min(start, spare.index[-1].normalize() - pd.Timedelta(days=days - 1))
    return start, start + pd.Timedelta(days=days) - pd.Timedelta(hours=1)


def draw(ax, spare, lv, shed, colors, steps, lv_lw=1.8):
    order = ordered(spare)
    x = spare.index
    bottom = np.zeros(len(x))
    kw_step = dict(step="post") if steps else {}
    for c in order:
        top = bottom + spare[c].values
        hatch = "////" if c in VRES_ORDER else None
        ax.fill_between(x, bottom, top, color=colors.get(c, "#bdbdbd"), lw=0, label=c,
                        alpha=0.55 if c not in FIRM_ORDER + STORAGE_ORDER else 0.95,
                        hatch=hatch, edgecolor="white" if hatch else None, **kw_step)
        bottom = top
    room = interconnector_room(spare).values
    if room.max() > 1.0:
        ax.plot(x, bottom + room, color=IC_COLOR, lw=0.9, ls="--", label=IC_LABEL,
                drawstyle="steps-post" if steps else "default")
    ax.fill_between(x, 0, -shed.values, color=SHED_COLOR, hatch="////", edgecolor=SHED_EDGE,
                    lw=0, label="Load shedding (below 0)", **kw_step)
    ax.plot(x, lv.values, color=LV_COLOR, lw=lv_lw, label=LV_LABEL,
            drawstyle="steps-post" if steps else "default")
    ax.axhline(0, color="#4d4d4d", lw=0.8)
    ax.set_ylabel("spare electricity capacity in DK2 (MW)")
    ax.grid(axis="y", color="#e0e0e0", lw=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.1), ncol=4, frameon=False, fontsize=9)


def shifted(obj, offset):
    out = obj.copy()
    out.index = out.index + pd.DateOffset(years=offset)
    return out


def plot_winter(spare, lv, shed, colors, offset, spans):
    sl = slice(*WINTER)
    s, l, h = (shifted(o.loc[sl], offset) for o in (spare, lv, shed))
    fig, ax = plt.subplots(figsize=(14, 5.5), constrained_layout=True)
    for start, end in spans:
        ax.axvspan(start + pd.DateOffset(years=offset), end + pd.DateOffset(years=offset),
                   color="#bdbdbd", alpha=0.18, lw=0, zorder=0)
    draw(ax, s, l, h, colors, steps=False, lv_lw=0.8)
    ax.set_xlim(s.index[0], s.index[-1])
    loc = mdates.MonthLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.text(0.995, 0.98, "hourly\nshaded: outage period" if spans else "hourly",
            transform=ax.transAxes,
            ha="right", va="top", fontsize=8, color="#4d4d4d")
    return fig


def plot_zoom(spare, lv, shed, colors, offset, window):
    sl = slice(*window)
    s, l, h = (shifted(o.loc[sl], offset) for o in (spare, lv, shed))
    fig, ax = plt.subplots(figsize=(14, 5.5), constrained_layout=True)
    draw(ax, s, l, h, colors, steps=False)
    ax.set_xlim(s.index[0], s.index[-1])
    loc = mdates.DayLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.text(0.995, 0.98, "hourly", transform=ax.transAxes, ha="right", va="top",
            fontsize=8, color="#4d4d4d")
    return fig


def outage_spans(cases_cfg, case):
    return [(pd.Timestamp(o["start"]), pd.Timestamp(o["end"]))
            for o in ((cases_cfg.get(case) or {}).get("interconnector_outages") or [])]


def main(*config_paths):
    cfg = load_config(config_paths or [CONFIG])["disruption"]
    opts = cfg.get("plotting", {})
    region, label = opts.get("region", "DK0 0AC"), opts.get("region_label", "DK2")
    out_dir = Path(cfg["output_directory"])
    for year in cfg["weather_years"]:
        offset = int(year[:4]) - MODEL_START_YEAR
        for case in cfg["cases"]:
            path = out_dir / year / case / "network_solved.nc"
            if not path.exists():
                continue
            print(f"loading {path}")
            n = pypsa.Network(path)
            colors = build_colors(n)
            spare, lv, shed = spare_capacity(n, region)
            d = out_dir / year / case / "plots" / "spare_capacity"
            d.mkdir(parents=True, exist_ok=True)
            stem = d / f"spare_capacity_{label}_{case}_{year}"
            pd.concat([spare, lv, shed.rename("Load shedding")], axis=1).round(1) \
                .to_csv(f"{stem}_hourly.csv", index_label="model_snapshot")
            fig = plot_winter(spare, lv, shed, colors, offset, outage_spans(cfg["cases"], case))
            fig.savefig(f"{stem}_winter.png", dpi=200, bbox_inches="tight")
            plt.close(fig)
            window = zoom_window(spare, shed)
            fig = plot_zoom(spare, lv, shed, colors, offset, window)
            fig.savefig(f"{stem}_zoom.png", dpi=200, bbox_inches="tight")
            plt.close(fig)
            print(f"wrote {stem}_{{winter,zoom}}.png, zoom {window[0]:%Y-%m-%d} (model date)")


if __name__ == "__main__":
    main(*sys.argv[1:])
