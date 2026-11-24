# SPDX-License-Identifier: MIT
"""Demand-bounded fuel shortfalls, distinct from upstream emergency fuel supply."""
from collections.abc import Mapping
from numbers import Real

import numpy as np
import pandas as pd


META_KEY = "fuel_demand_shedding"
PREFIX = "load-shedding-fuel-"
# Bus carriers, not Link name substring matches. Only actual Loads are eligible.
DEMAND_FUELS = {
    "gas": "gas", "gas for industry": "gas",
    "oil": "oil", "oil primary": "oil", "land transport oil": "oil",
    "kerosene for aviation": "oil", "shipping oil": "oil",
    "agriculture machinery oil": "oil", "naphtha for industry": "oil",
    "biomass": "biomass", "solid biomass": "biomass",
    "solid biomass for industry": "biomass",
    "biogas": "biogas", "unsustainable bioliquids": "biomass",
    "coal": "coal", "coal for industry": "coal", "lignite": "lignite",
    "methanol": "methanol", "industry methanol": "methanol",
    "shipping methanol": "methanol", "H2": "hydrogen", "NH3": "ammonia",
}
DETAIL_COLUMNS = [
    "generator", "load", "bus", "fuel", "demand_carrier", "region", "country",
    "penalty_EUR_per_MWh", "demand_MWh", "unserved_MWh", "served_MWh",
    "unserved_fraction", "peak_unserved_MW", "shedding_hours", "penalty_cost_EUR",
]
TOTAL_COLUMNS = ["fuel", "demand_MWh", "unserved_MWh", "served_MWh",
                 "unserved_fraction", "peak_unserved_MW", "shedding_hours", "penalty_cost_EUR"]


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not np.isfinite(value):
        raise ValueError(f"{label} must be a finite number.")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive.")
    return float(value)


def add_fuel_demand_shedding(n, options=None):
    """Add one nonnegative, hourly demand-bounded slack Generator per fuel Load.

    Prices are EUR/MWh of unmet fuel demand. They do not price upstream fuel
    purchases or create emissions for fuel that was never consumed.
    """
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise ValueError("load_shedding.fuel must be a mapping.")
    allowed = {"enable", "default_cost", "costs_by_fuel", "costs_by_carrier",
               "costs_by_load", "regional_cost_adders", "demand_carriers"}
    if set(options) - allowed:
        raise ValueError(f"Unknown fuel shedding settings: {sorted(set(options) - allowed)}")
    if not isinstance(options.get("enable", False), bool):
        raise ValueError("fuel shedding enable must be boolean.")
    old = n.generators.index[n.generators.carrier.str.startswith(PREFIX, na=False)]
    if not options.get("enable", False):
        n.remove("Generator", old)
        n.meta = {**n.meta, META_KEY: {}}
        return

    default = number(options.get("default_cost", 23_300), "default_cost", positive=True)
    maps = {}
    for key in allowed - {"enable", "default_cost"}:
        value = options.get(key, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"{key} must be a mapping.")
        maps[key] = dict(value)
        for name, cost in value.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{key} requires nonempty string keys.")
            if key == "demand_carriers":
                if not isinstance(cost, str) or not cost:
                    raise ValueError("demand_carriers must map bus carriers to nonempty fuel names.")
            else:
                number(cost, f"{key}.{name}", positive=key != "regional_cost_adders")
    unknown_loads = set(maps["costs_by_load"]) - set(n.loads.index)
    if unknown_loads:
        raise ValueError(f"Unknown fuel shedding Load overrides: {sorted(unknown_loads)}")
    fuels = {**DEMAND_FUELS, **maps["demand_carriers"]}
    demand = n.get_switchable_as_dense("Load", "p_set").mul(-n.loads.sign, axis=1)
    profiles, records = {}, {}
    for load, row in n.loads.iterrows():
        bus = n.buses.loc[row.bus]
        if bus.carrier not in fuels:
            continue
        values = demand[load]
        if not np.isfinite(values.to_numpy()).all():
            raise ValueError(f"Nonfinite fuel demand: {load}")
        values = values.clip(lower=0)
        peak = float(values.max())
        if peak <= 0:
            continue
        fuel = fuels[bus.carrier]
        region = bus.get("location", "") or bus.get("country", "") or row.bus
        cost = maps["costs_by_load"].get(load, maps["costs_by_carrier"].get(
            bus.carrier, maps["costs_by_fuel"].get(fuel, default)))
        cost += maps["regional_cost_adders"].get(region, 0.0)
        number(cost, f"Final fuel shedding cost for {load}", positive=True)
        name = f"{load} load shedding fuel"
        if name in n.generators.index and name not in old:
            raise ValueError(f"Fuel shedding Generator name already exists: {name}")
        profiles[name] = values / peak
        records[name] = {"load": load, "bus": row.bus, "fuel": fuel,
                         "demand_carrier": bus.carrier, "region": region,
                         "country": bus.get("country", ""),
                         "penalty_EUR_per_MWh": float(cost), "capacity_MW": peak}

    # Validation completes before refreshing our own assets; never resize Links.
    n.remove("Generator", old)
    for name, record in records.items():
        carrier = PREFIX + record["fuel"]
        if carrier not in n.carriers.index:
            n.add("Carrier", carrier, nice_name=f"Unserved {record['fuel']} demand")
        n.add("Generator", name, bus=record["bus"], carrier=carrier,
              p_nom=record["capacity_MW"], p_nom_extendable=False,
              p_min_pu=0.0, p_max_pu=profiles[name], sign=1.0,
              marginal_cost=record["penalty_EUR_per_MWh"])
    n.meta = {**n.meta, META_KEY: records}


def fuel_shedding_reports(n):
    """Report saved dispatch snapshots only (committed snapshots in rolling mode)."""
    records = n.meta.get(META_KEY, {})
    names = list(records)
    power = n.generators_t.p.reindex(index=n.snapshots, columns=names, fill_value=0)
    if not np.isfinite(power.to_numpy()).all():
        raise ValueError("Missing/nonfinite fuel shedding dispatch.")
    power = power.clip(lower=0)
    demand = n.get_switchable_as_dense("Load", "p_set").mul(-n.loads.sign, axis=1).clip(lower=0)
    weights = n.snapshot_weightings.generators
    cost_weights = n.snapshot_weightings.objective
    costs = n.get_switchable_as_dense("Generator", "marginal_cost")
    rows = []
    for name, record in records.items():
        values = power[name]
        requirement = demand[record["load"]]
        if (values - requirement > 1e-4).any():
            raise ValueError(f"Fuel shedding exceeds demand: {name}")
        energy = float(values.mul(weights).sum())
        total = float(requirement.mul(weights).sum())
        rows.append({"generator": name, **{k: v for k, v in record.items() if k != "capacity_MW"},
                     "demand_MWh": total, "unserved_MWh": energy,
                     "served_MWh": max(0.0, total - energy),
                     "unserved_fraction": energy / total if total else 0.0,
                     "peak_unserved_MW": float(values.max()),
                     "shedding_hours": float(weights.loc[values > 1e-6].sum()),
                     "penalty_cost_EUR": float((values * costs[name] * cost_weights).sum())})
    detail = pd.DataFrame(rows, columns=DETAIL_COLUMNS)
    totals = []
    for fuel, group in detail.groupby("fuel"):
        fields = group[["demand_MWh", "unserved_MWh", "served_MWh", "penalty_cost_EUR"]].sum().to_dict()
        series = power[group.generator].sum(axis=1)
        totals.append({"fuel": fuel, **fields,
                       "unserved_fraction": fields["unserved_MWh"] / fields["demand_MWh"] if fields["demand_MWh"] else 0,
                       "peak_unserved_MW": float(series.max()),
                       "shedding_hours": float(weights.loc[series > 1e-6].sum())})
    return {"fuel_shedding_by_demand": detail,
            "fuel_shedding_by_fuel": pd.DataFrame(totals, columns=TOTAL_COLUMNS),
            "fuel_shedding_timeseries": power.rename_axis("snapshot").reset_index()}
