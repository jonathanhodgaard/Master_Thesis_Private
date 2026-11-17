"""Shared helpers for the interconnector-outage plots (DK2 resilience).

NOTE: there are two copies of this module, keep them in sync:
  - outage_helpers.py           used by the notebooks in the repository root
  - scripts/outage_helpers.py   used by the Snakemake plot rules (rules/plot_disruption.smk)

Regional energy balance of a solved PyPSA network, grouped into technology groups,
plus PyPSA-Eur based colours. Used by base_network_dispatch.ipynb and
missing_demand_plots.ipynb:

    from outage_helpers import *
    COLORS = build_colors(n)
    bal = region_balance(n, "DK0 0AC", "power")

Sign convention: every column of a balance is the hourly net injection (MW) into the
region's power or heat buses. Above zero = supply, below zero = use; each hour sums to ~0.
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd

__all__ = [
    "POWER_CARRIERS", "HEAT_CARRIERS", "INTERCONNECTOR_NAMES", "KIND_TITLE",
    "POWER_RULES", "HEAT_RULES", "DEMAND_GROUP", "SUPPLY_ORDER", "USE_ORDER",
    "STORAGE_GROUPS", "GROUP_CARRIER", "COLOR_OVERRIDES", "FALLBACK_COLORS", "IC_COLORS",
    "region_buses", "injections", "build_colors", "is_interconnector",
    "interconnector_groups", "group_of", "region_balance", "missing_demand", "save",
]

# ---- bus sets ------------------------------------------------------------------
POWER_CARRIERS = ["AC", "low voltage"]
HEAT_CARRIERS = ["urban central heat", "urban decentral heat", "rural heat"]  # incl. household buses

# (region, far-end bus) -> display name. Unknown pairs fall back to "Interconnectors <CC>".
# Naming: DK2 (Zealand) is bus "DK0 0AC"; "DK2 0AC" is actually DK1 (Jutland).
INTERCONNECTOR_NAMES = {
    ("DK0 0AC", "SE0 0AC"): "Øresund (SE)",
    ("DK0 0AC", "DE2 0AC"): "Kontek (DE)",
    ("DK0 0AC", "DK2 0AC"): "Great Belt (DK1)",
}
KIND_TITLE = {"power": "Electricity", "heat": "Heat"}


def region_buses(n, region, carriers):
    b = n.buses
    return b.index[(b.location == region) & b.carrier.isin(carriers)]


def injections(n, buses):
    # Hourly net injection (MW) into a set of buses, one column per component.
    # Flows between two buses inside the set cancel (only their losses remain).
    # Returns (inj, meta); meta has component, carrier and far_bus (for branches
    # that leave the region's AC network = interconnectors).
    buses = set(buses)
    frames, meta = [], []

    for c in ["Generator", "Load", "StorageUnit", "Store"]:
        comp = n.components[c]
        sel = comp.static.index[comp.static.bus.isin(buses)]
        if sel.empty:
            continue
        p = comp.dynamic["p"].reindex(columns=sel, fill_value=0.0)
        frames.append(p * comp.static.loc[sel, "sign"])
        meta.append(pd.DataFrame({"component": c, "carrier": comp.static.loc[sel, "carrier"],
                                  "far_bus": None}, index=sel))

    for c in ["Link", "Line", "Transformer"]:
        comp = n.components[c]
        st = comp.static
        if st.empty:
            continue
        ports = sorted(int(k[3:]) for k in st.columns if re.fullmatch(r"bus\d+", k))
        inside = {i: st[f"bus{i}"].isin(buses) for i in ports}
        touching = st.index[np.logical_or.reduce([inside[i] for i in ports])]
        if touching.empty:
            continue
        net = pd.DataFrame(0.0, index=n.snapshots, columns=touching)
        for i in ports:
            names = st.index[inside[i]]
            if names.empty or f"p{i}" not in comp.dynamic:
                continue
            net[names] -= comp.dynamic[f"p{i}"].reindex(columns=names, fill_value=0.0)
        # far bus: an AC bus outside the set at the other end of an AC/DC branch
        far = pd.Series(None, index=touching, dtype=object)
        for i in ports:
            b = st.loc[touching, f"bus{i}"]
            out = ~inside[i].loc[touching] & b.map(n.buses.carrier).eq("AC")
            far[out & st.loc[touching, "carrier"].isin(["AC", "DC"])] = b[out]
        frames.append(net)
        meta.append(pd.DataFrame({"component": c, "carrier": st.loc[touching, "carrier"],
                                  "far_bus": far}, index=touching))

    inj = pd.concat(frames, axis=1)
    meta = pd.concat(meta)
    inj.columns = meta.index = [f"{c}::{i}" for c, i in zip(meta.component, meta.index)]
    return inj, meta


# ---- grouping ------------------------------------------------------------------
# First matching regex wins. Loads always go to the demand group, interconnectors get one
# group per cable (forward + reversed link summed). Unmatched -> "Other" (printed).
POWER_RULES = [
    (r"load-shedding", "Load shedding"),
    (r"offwind", "Offshore wind"),
    (r"onwind", "Onshore wind"),
    (r"solar", "Solar"),
    (r"ror|hydro|PHS", "Hydro"),
    (r"nuclear", "Nuclear"),
    (r"gas CHP", "Gas CHP"),
    (r"biomass CHP", "Biomass CHP"),
    (r"waste CHP", "Waste CHP"),
    (r"^biomass$", "Biomass power"),       # electricity-only biomass plant
    (r"oil", "Oil"),
    (r"OCGT|CCGT", "Gas turbines"),
    (r"coal|lignite", "Coal"),
    (r"heat pump|resistive heater", "Power-to-heat"),
    (r"BEV charger|V2G", "EV charging"),
    (r"battery", "Battery"),
    (r"H2|[Ee]lectrolysis|[Ff]uel [Cc]ell", "Hydrogen"),
    (r"distribution grid", "Electricity demand"),   # grid losses
]
HEAT_RULES = [
    (r"load-shedding", "Load shedding"),
    (r"gas CHP", "Gas CHP"),
    (r"biomass CHP", "Biomass CHP"),
    (r"waste CHP", "Waste CHP"),
    (r"coal CHP", "Coal"),
    (r"oil CHP", "Oil"),
    (r"gas boiler", "Gas boilers"),
    (r"biomass boiler", "Biomass boilers"),
    (r"oil boiler", "Oil boilers"),
    (r"heat pump", "Heat pumps"),
    (r"resistive heater", "Resistive heaters"),
    (r"water tanks|water pits", "Thermal storage"),
    (r"household heat delivery", "internal"),        # nets to 0 inside the heat set
]
DEMAND_GROUP = {"power": "Electricity demand", "heat": "Heat demand"}

# bottom-up order of the supply stack and top-down order of the use stack
SUPPLY_ORDER = ["Nuclear", "Hydro", "Waste CHP", "Biomass CHP", "Biomass power", "Gas CHP",
                "Coal", "Oil", "Gas turbines", "Gas boilers", "Biomass boilers", "Oil boilers",
                "Heat pumps", "Resistive heaters",
                "Offshore wind", "Onshore wind", "Solar", "Battery", "Hydrogen", "Thermal storage",
                "<interconnectors>", "Other", "Load shedding"]
USE_ORDER = ["Electricity demand", "Heat demand", "Power-to-heat", "EV charging", "Hydrogen",
             "Battery", "Thermal storage", "<interconnectors>", "Other"]

# storage only shifts energy that something else supplied: not a source, not a demand
STORAGE_GROUPS = ["Thermal storage", "Battery"]


# ---- colours -------------------------------------------------------------------
# Technology groups use PyPSA-Eur's tech_colors as stored in n.carriers.color,
# via one representative carrier per group. Exceptions in COLOR_OVERRIDES.
GROUP_CARRIER = {
    "Offshore wind": "offwind-ac", "Onshore wind": "onwind", "Solar": "solar",
    "Hydro": "hydro", "Nuclear": "nuclear",
    "Gas CHP": "urban central gas CHP", "Biomass CHP": "urban central solid biomass CHP",
    "Waste CHP": "waste CHP", "Biomass power": "biomass",
    "Gas turbines": "OCGT", "Coal": "coal",
    "Gas boilers": "urban decentral gas boiler", "Biomass boilers": "urban decentral biomass boiler",
    "Heat pumps": "urban central air heat pump", "Power-to-heat": "urban central air heat pump",
    "Resistive heaters": "urban central resistive heater",
    "EV charging": "BEV charger", "Battery": "home battery", "Hydrogen": "H2 Electrolysis",
    "Thermal storage": "urban central water tanks discharger",
}
COLOR_OVERRIDES = {
    # demand neutral so it doesn't compete with supply or shedding
    # (PyPSA-Eur: electricity navy #110d63, heat demand red #cc1f1f ~ shedding red)
    "Electricity demand": "#a6a6a6", "Heat demand": "#a6a6a6",
    "Load shedding": "#dd2e23",          # PyPSA-Eur 'load' (Load Shedding); hatched in plots
    # PyPSA-Eur oil greys (#c9c9c9, #adadad) vanish against grey demand
    "Oil": "#262626", "Oil boilers": "#5f5f5f",
    # interconnectors: PyPSA-Eur logic (AC green, DC purple), one shade per cable
    "Øresund (SE)": "#70af1d",           # AC line, PyPSA-Eur 'AC'
    "Kontek (DE)": "#8a1caf",            # DC link, PyPSA-Eur 'DC'
    "Great Belt (DK1)": "#c48be3",       # DC link, lighter purple
    # adjacent PyPSA-Eur greens are too similar: heat pump #6cfb6b vs resistive heater
    # #8cdf85 / BEV charger #baf238
    "Resistive heaters": "#d8f9b8",      # PyPSA-Eur generic 'resistive heater'
    "EV charging": "#5fb3b3",
    "Biomass power": "#5f5b1d",          # PyPSA-Eur #baa741 ~ biomass CHP #9d9042
    "Other": "#d9d9d9",
}
FALLBACK_COLORS = ["#7f7f7f", "#bcbd22", "#17becf"]
# interconnectors without an entry in COLOR_OVERRIDES take these, in order
IC_COLORS = ["#5e3c99", "#c2185b", "#00897b", "#3949ab", "#ef6c00", "#6d4c41"]


def build_colors(n):
    carrier_color = n.carriers.color.replace("", np.nan).dropna()
    colors = {g: carrier_color[c] for g, c in GROUP_CARRIER.items() if c in carrier_color}
    missing = sorted(set(GROUP_CARRIER) - set(colors))
    if missing:
        print("no PyPSA-Eur colour in this network for:", missing)
    return {**colors, **COLOR_OVERRIDES}


# ---- balance -------------------------------------------------------------------
def is_interconnector(group):
    return group in INTERCONNECTOR_NAMES.values() or group.startswith("Interconnectors ")


def interconnector_groups(bal):
    return [g for g in bal if is_interconnector(g)]


def group_of(row, kind, region):
    if row.component == "Load":
        return DEMAND_GROUP[kind]
    if pd.notna(row.far_bus) and kind == "power":
        return INTERCONNECTOR_NAMES.get((region, row.far_bus),
                                        f"Interconnectors {row.far_bus[:2]}")
    for pattern, group in (POWER_RULES if kind == "power" else HEAT_RULES):
        if re.search(pattern, row.carrier):
            return group
    return "Other"


def region_balance(n, region, kind, shed_noise_mw=0.1):
    # Hourly balance (MW) of the region's power or heat buses, one column per group.
    # Hourly load shedding below shed_noise_mw is solver noise and set to 0.
    carriers = POWER_CARRIERS if kind == "power" else HEAT_CARRIERS
    inj, meta = injections(n, region_buses(n, region, carriers))
    groups = meta.apply(group_of, axis=1, kind=kind, region=region)
    other = meta.carrier[(groups == "Other") & inj.abs().sum().gt(0)].unique()
    if len(other):
        print(f"[{kind}] carriers in 'Other': {sorted(other)}")
    bal = inj.T.groupby(groups).sum().T.drop(columns="internal", errors="ignore")
    if "Load shedding" in bal:
        bal["Load shedding"] = bal["Load shedding"].where(bal["Load shedding"] > shed_noise_mw, 0.0)
    return bal


def missing_demand(bal):
    # Hourly unmet demand of a region balance.
    #   demand = everything used in the region except exports and storage charging
    #            (power: fixed demand + power-to-heat + EVs + H2 + grid losses; heat: heat demand)
    #   shed   = load shedding (the part of the demand that is not met)
    # Returns a DataFrame with shed_MW, demand_MW and missing_pct = shed / demand * 100.
    used = bal.drop(columns=interconnector_groups(bal) + STORAGE_GROUPS, errors="ignore")
    demand = -used.clip(upper=0).sum(axis=1)
    shed = bal["Load shedding"] if "Load shedding" in bal else pd.Series(0.0, index=bal.index)
    pct = (shed / demand.where(demand > 0) * 100).fillna(0.0)
    return pd.DataFrame({"shed_MW": shed, "demand_MW": demand, "missing_pct": pct})


def save(fig, name, fig_dir=Path("results/plots_outage")):
    fig_dir = Path(fig_dir)
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(fig_dir / f"{name}.png", dpi=200, bbox_inches="tight")
    fig.savefig(fig_dir / f"{name}.pdf", bbox_inches="tight")
