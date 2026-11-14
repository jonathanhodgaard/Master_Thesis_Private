# SPDX-License-Identifier: MIT
"""Disruption-only uniform material arrivals and bounded waste/HVC backlog."""
import numpy as np


def prepare_waste_hvc(n, options):
    if options.get("mode", "uniform_arrivals") != "uniform_arrivals":
        raise ValueError("Supported rolling waste/HVC mode is uniform_arrivals.")
    if "rolling_waste_hvc" in n.meta:
        raise ValueError("Waste/HVC arrivals have already been prepared.")
    hours = float(n.snapshot_weightings.generators.sum())
    backlog_hours = float(options.get("backlog_hours", 48))
    initial = float(options.get("initial_backlog_MWh", 0))
    if not np.isfinite([backlog_hours, initial]).all() or min(backlog_hours, initial) < 0:
        raise ValueError("Waste backlog hours and initial energy must be finite and nonnegative.")
    waste = n.generators.index[n.generators.carrier.eq("municipal solid waste")]
    global_waste = n.global_constraints.loc[
        n.global_constraints.type.eq("operational_limit")
        & n.global_constraints.carrier_attribute.eq("municipal solid waste")
    ]
    quantities = n.generators.loc[waste, "e_sum_max"].copy()
    if not global_waste.empty:
        if len(global_waste) != 1:
            raise ValueError("Expected one aggregate municipal waste allowance.")
        annual = n.generators_t.p.reindex(index=n.snapshots, columns=waste, fill_value=0).sum()
        total = float(global_waste.constant.iloc[0])
        if total > 0 and annual.sum() <= 0:
            raise ValueError("Missing reference waste dispatch for regional arrival allocation.")
        quantities = annual / annual.sum() * total if annual.sum() else annual * 0
    if not np.isfinite(quantities).all() or quantities.lt(0).any():
        raise ValueError("Waste arrivals require finite nonnegative annual quantities.")
    rates = quantities / hours
    for name, rate in rates.items():
        capacity = float(n.generators.at[name, "p_nom"])
        if rate > capacity + 1e-6:
            raise ValueError(f"Waste arrivals exceed fixed supply capacity for {name}.")
        fraction = rate / capacity if capacity > 0 else 0
        n.generators_t.p_min_pu[name] = fraction
        n.generators_t.p_max_pu[name] = fraction
    n.generators.loc[waste, "e_sum_min"] = -np.inf
    n.generators.loc[waste, "e_sum_max"] = np.inf
    n.remove("GlobalConstraint", global_waste.index)
    stores = n.stores.index[n.stores.carrier.eq("non-sequestered HVC")]
    if len(waste) and rates.sum() > 0 and stores.empty:
        raise ValueError("Municipal waste arrivals require the shared HVC treatment system.")
    disposal_updates = []
    for name in stores:
        bus = n.stores.at[name, "bus"]
        quantity = float(n.stores.at[name, "e_initial"])
        rate = quantity / hours
        incoming = 0.0
        for _, link in n.links.loc[n.links.bus1.eq(bus) & n.links.carrier.eq("municipal solid waste")].iterrows():
            incoming += rates.reindex(n.generators.index[n.generators.bus.eq(link.bus0)], fill_value=0).sum() * link.efficiency
        capacity = float((rate + incoming) * backlog_hours)
        if initial > capacity + 1e-6:
            raise ValueError(f"Initial waste backlog exceeds capacity for {name}.")
        n.add("Generator", name + " material arrivals", bus=bus, carrier="HVC material arrivals",
              p_nom=rate, p_min_pu=1, p_max_pu=1, p_nom_extendable=False, marginal_cost=0)
        n.stores.loc[name, ["e_nom", "e_nom_opt", "e_initial", "e_cyclic"]] = [capacity, capacity, initial, False]
        n.stores_t.e_max_pu[name] = 1.0
        n.stores_t.e_min_pu[name] = 0.0
        if options.get("require_empty_at_period_end", True):
            # Only the true accounting-year end, not an early test/report cutoff.
            n.stores_t.e_max_pu.loc[n.snapshots[-1], name] = 0.0
        # HVC to air is carbon-accounting disposal/decay, not installed waste
        # incineration capacity. Freezing its tiny full-year optimized rating
        # would force uniform arrivals into heat-producing plants in summer.
        # Permit incoming material plus the whole bounded backlog to be released
        # within one hourly interval, including at the true year-end deadline.
        disposal = n.links.index[
            n.links.bus0.eq(bus) & n.links.carrier.eq("HVC to air")
            & n.links.bus1.map(n.buses.carrier).eq("co2")
        ]
        required = rate + incoming + capacity
        if required > 0 and disposal.empty:
            raise ValueError(f"Missing HVC to air accounting disposal route at {bus}.")
        for link in disposal:
            previous = float(n.links.at[link, "p_nom"])
            rating = max(previous, required)
            n.links.loc[link, ["p_nom", "p_nom_opt", "p_nom_min", "p_nom_max"]] = rating
            n.links.at[link, "p_nom_extendable"] = False
            disposal_updates.append({"link": link, "previous_input_capacity_MW": previous,
                                     "input_capacity_MW": rating})
    if "HVC material arrivals" not in n.carriers.index and len(stores):
        n.add("Carrier", "HVC material arrivals")
    n.meta = {**n.meta, "rolling_waste_hvc": {"stores": stores.tolist(),
              "mode": "uniform_arrivals", "backlog_hours": backlog_hours,
              "accounting_disposal_links": disposal_updates,
              "accounting_end": str(n.snapshots[-1])}}
