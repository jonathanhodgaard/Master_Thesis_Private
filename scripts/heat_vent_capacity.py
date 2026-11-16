# SPDX-License-Identifier: MIT
"""Size existing heat-disposal sinks from connected heat-input power capacities."""
import numpy as np
import pandas as pd

META_KEY = "heat_vent_capacity"


def heat_capacity_sources(n, buses):
    """Return MW_heat per nominal MW for every heat-injecting component.

    Use maximum output over the complete input calendar, including varying
    efficiencies and reverse-operated heat pumps. Sum multiple ports on the
    same bus before taking the positive injection. Storage-discharge Links
    count as incoming power; a Store's MWh rating does not. Demand, heat
    shedding and disposal sinks never contribute to installed heat capacity.
    """
    sources = {bus: [] for bus in buses}

    def record(component, asset, bus, factor):
        if not np.isfinite(factor):
            raise ValueError(f"Non-finite heat capacity coefficient for {component} {asset}.")
        if factor > 0:
            sources[bus].append({"component": component, "asset": asset,
                                 "factor_MW_heat_per_MW": float(factor)})

    for component in ("Generator", "StorageUnit"):
        table = n.components[component].static
        selected = table.index[table.bus.isin(buses) & table.active]
        if component == "Generator":
            excluded = table.carrier.str.contains(r"heat vent$|load[- ]?shedding", case=False, na=False)
            selected = selected.difference(table.index[excluded])
        if selected.empty:
            continue
        lower = n.get_switchable_as_dense(component, "p_min_pu", inds=selected)
        upper = n.get_switchable_as_dense(component, "p_max_pu", inds=selected)
        for asset in selected:
            sign = table.at[asset, "sign"]
            factor = np.maximum(lower[asset] * sign, upper[asset] * sign).clip(lower=0).to_numpy().max()
            record(component, asset, table.at[asset, "bus"], factor)

    ports = [column for column in n.links if column.startswith("bus") and column[3:].isdigit()]
    selected = n.links.index[n.links[ports].isin(buses).any(axis=1) & n.links.active]
    if selected.empty:
        return sources
    lower = n.get_switchable_as_dense("Link", "p_min_pu", inds=selected)
    upper = n.get_switchable_as_dense("Link", "p_max_pu", inds=selected)
    efficiencies = {
        port: n.get_switchable_as_dense(
            "Link", "efficiency" if port == "bus1" else "efficiency" + port[3:], inds=selected,
        ) for port in ports if port != "bus0"
    }
    for asset in selected:
        row = n.links.loc[asset]
        for bus in set(row[ports]).intersection(buses):
            coefficient = pd.Series(0., index=n.snapshots)
            for port in ports:
                if row[port] == bus:
                    coefficient += -1. if port == "bus0" else efficiencies[port][asset]
            factor = np.maximum(coefficient * lower[asset], coefficient * upper[asset]).clip(lower=0).to_numpy().max()
            record("Link", asset, bus, factor)
    return sources


def prepare_heat_vent_capacity(n, investment=False):
    """Give each enabled heat vent the sum of its bus's heat-input capacities.

    Planning mode ties the vent rating to capacity variables after model
    creation. Dispatch mode evaluates the same sum from frozen capacities.
    Only existing vents are changed, respecting heat_vent area switches and
    household topology. This implements heat rejection as an accounting sink,
    with its existing per-MWh disposal penalty and no investment cost.
    """
    vents = n.generators.index[n.generators.carrier.str.endswith(" heat vent") & n.generators.active]
    if vents.empty:
        return
    if isinstance(n.snapshots, pd.MultiIndex):
        raise ValueError("Heat-vent capacity sizing currently supports single-period networks.")
    buses = n.generators.loc[vents, "bus"]
    if buses.duplicated().any():
        raise ValueError("Expected only one heat vent per bus.")
    sources = heat_capacity_sources(n, set(buses))
    records = {}
    for vent, bus in buses.items():
        capacity = 0.
        for source in sources[bus]:
            table = n.components[source["component"]].static
            row = table.loc[source["asset"]]
            if not investment and row.p_nom_extendable:
                raise ValueError("Freeze heat-source capacities before sizing dispatch heat vents.")
            nominal = row.p_nom
            if not np.isfinite(nominal) or nominal < 0:
                raise ValueError(f"Invalid installed heat-source capacity: {source['asset']}")
            capacity += nominal * source["factor_MW_heat_per_MW"]
        records[vent] = {"bus": bus, "sources": sources[bus],
                         "capacity_MW_heat": None if investment else float(capacity)}

    # Validate everything above before changing the network.
    for vent in vents:
        capacity = records[vent]["capacity_MW_heat"]
        n.generators.loc[vent, ["p_nom", "p_nom_min", "p_nom_max"]] = (
            [0., 0., np.inf] if investment else [capacity, capacity, capacity]
        )
        n.generators.at[vent, "p_nom_extendable"] = investment
        if not investment:
            n.generators.at[vent, "p_nom_opt"] = capacity
        n.generators.loc[vent, ["sign", "p_min_pu", "p_max_pu", "capital_cost"]] = [1., -1., 0., 0.]
        for attribute, value in (("p_min_pu", -1.), ("p_max_pu", 0.)):
            if vent in n.generators_t[attribute]:
                n.generators_t[attribute].loc[:, vent] = value
    n.meta = {**(n.meta or {}), META_KEY: {
        "mode": "investment" if investment else "dispatch",
        "basis": "sum of maximum connected heat-input power capacities; excludes shedding",
        "vents": records,
    }}


def add_heat_vent_capacity_constraints(n):
    """Tie planning vent ratings to installed fixed/extendable heat sources."""
    settings = (n.meta or {}).get(META_KEY, {})
    if settings.get("mode") != "investment":
        return

    def capacity(component, asset):
        row = n.components[component].static.loc[asset]
        if not row.p_nom_extendable:
            return float(row.p_nom)
        variable = n.model[f"{component}-p_nom"]
        dimension, = variable.dims
        return variable.sel({dimension: asset}, drop=True)

    for number, (vent, record) in enumerate(settings["vents"].items()):
        total = sum(
            capacity(source["component"], source["asset"]) * source["factor_MW_heat_per_MW"]
            for source in record["sources"]
        )
        n.model.add_constraints(
            capacity("Generator", vent) == total,
            name=f"Heat-vent-capacity-{number}",
        )
