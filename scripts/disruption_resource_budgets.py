# SPDX-License-Identifier: MIT
"""Reference fuel prices and optional remaining-allowance constraints."""
from dataclasses import dataclass

import numpy as np
import xarray as xr

from scripts.resource_budget_duals import FUEL_CARRIERS


@dataclass
class ResourceBudget:
    identifier: str
    carrier: str
    generators: tuple
    allowance_MWh: float
    remaining_MWh: float
    scarcity_price: float
    enforce: bool
    price_enabled: bool
    used_before_MWh: float = 0.0
    used_run_MWh: float = 0.0


def resource_options(options, carrier):
    overrides = options.get("overrides", {}).get(carrier, {})
    return (overrides.get("enforce_allowance", options.get("enforce_allowances", False)),
            overrides.get("apply_scarcity_price", options.get("apply_scarcity_prices", True)))


def scarcity_price(dual, name):
    if not np.isfinite(dual) or dual > 1e-5:
        raise ValueError(f"Invalid upper-budget dual for {name}: {dual}")
    return max(0.0, -float(dual))


def build_resource_budgets(n, reference, options, start):
    """Read allowances before removing native annual constraints from the copy."""
    records = {(r["constraint"], r["asset"]): r for r in reference.meta.get("resource_budget_duals", [])}
    previous = reference.snapshots[reference.snapshots < start]
    budgets = []

    def add(identifier, carrier, names, limit, key, used_before):
        enforce, price_enabled = resource_options(options, carrier)
        if price_enabled and limit > 0:
            record = records.get(key)
            if record is None:
                raise ValueError(f"Missing exported fuel-budget dual for {key}; rebuild and solve the matching base network first.")
            if record["sense"] != "<=" or not np.isclose(record["rhs_MWh"], limit):
                raise ValueError(f"Reference fuel-budget dual does not match allowance: {key}")
            price = scarcity_price(record["raw_dual"], identifier)
        else:
            price = 0.0
        remaining = float(limit - used_before)
        if enforce and remaining < -1e-4:
            raise ValueError(f"Reference already exceeds resource allowance: {identifier}")
        budgets.append(ResourceBudget(identifier, carrier, tuple(names), float(limit), remaining,
                                      price, bool(enforce), bool(price_enabled), float(used_before)))

    fuels = reference.generators.loc[reference.generators.carrier.isin(FUEL_CARRIERS)]
    fuels = fuels.loc[reference.components["Generator"].get_active_assets().reindex(fuels.index)]
    if fuels.e_sum_min.gt(0).any():
        raise ValueError("Reference still requires compulsory biomass supply; rebuild with enforce_minimum_use: false.")
    for asset, row in fuels.iterrows():
        if np.isfinite(row.e_sum_max):
            used = reference.generators_t.p.reindex(index=previous, columns=[asset], fill_value=0).sum().sum()
            add(f"Generator:{asset}", row.carrier, [asset], row.e_sum_max,
                ("Generator-e_sum_max", asset), used)
    global_fuels = reference.global_constraints.loc[
        reference.global_constraints.type.eq("operational_limit")
        & reference.global_constraints.carrier_attribute.isin(FUEL_CARRIERS)
    ]
    for asset, row in global_fuels.iterrows():
        if row.sense != "<=":
            raise ValueError(f"Reference fuel budget {asset} is not an upper allowance.")
        names = fuels.index[fuels.carrier.eq(row.carrier_attribute)]
        used = reference.generators_t.p.reindex(index=previous, columns=names, fill_value=0).sum().sum()
        add(f"GlobalConstraint:{asset}", row.carrier_attribute, names, row.constant,
            (f"GlobalConstraint-{asset}", asset), used)
    n.generators.loc[fuels.index, "e_sum_min"] = 0
    n.generators.loc[fuels.index, "e_sum_max"] = np.inf
    n.remove("GlobalConstraint", global_fuels.index)

    # The old import Store is a bookkeeping fuel source, not physical storage.
    # Replace it so price-only mode can genuinely exceed its annual allowance.
    for asset in reference.stores.index[reference.stores.carrier.eq("solid biomass import")]:
        row = reference.stores.loc[asset]
        carrier = row.carrier
        enforce, price_enabled = resource_options(options, carrier)
        price = 0.0
        if price_enabled and row.e_initial > 0:
            duals = reference.stores_t.get("mu_energy_balance")
            if duals is None or asset not in duals or not np.isfinite(duals[asset]).all():
                raise ValueError(f"Missing reference import-stock duals for {asset}.")
            # Store balance dual is the value of extra stock, excluding the
            # existing Store dispatch purchase cost (verified by sensitivity test).
            price = float(duals[asset].mean())
            if price < -1e-5:
                raise ValueError(f"Unexpected negative import-stock scarcity value for {asset}.")
            price = max(0.0, price)
        costs = n.get_switchable_as_dense("Store", "marginal_cost")[asset].copy()
        limit = float(row.e_initial)
        used = float(reference.stores_t.p.reindex(index=previous, columns=[asset], fill_value=0).sum().sum())
        links = n.links.index[n.links.bus0.eq(row.bus)]
        power = float(n.links.loc[links, "p_nom"].sum())
        if not np.isfinite(power):
            raise ValueError("Import conversion requires finite fixed delivery capacities.")
        name = asset + " rolling supply"
        n.remove("Store", asset)
        n.add("Generator", name, bus=row.bus, carrier=carrier, p_nom=power,
              p_nom_extendable=False, marginal_cost=costs)
        budgets.append(ResourceBudget(f"Inventory:{asset}", carrier, (name,), limit,
                                      limit - used, price, bool(enforce), bool(price_enabled), used))
    return budgets


def add_resource_terms(n, snapshots, budgets):
    weights = xr.DataArray(n.snapshot_weightings.generators.loc[snapshots].to_numpy(),
                           dims=["snapshot"], coords={"snapshot": snapshots})
    for number, budget in enumerate(budgets):
        if not budget.generators:
            continue
        energy = (n.model["Generator-p"].sel(snapshot=snapshots, name=list(budget.generators)) * weights).sum()
        # A zero reference allowance always disables the source, even in price-only mode.
        if budget.enforce or budget.allowance_MWh == 0:
            if budget.remaining_MWh < -1e-4:
                raise ValueError(f"Resource allowance exhausted: {budget.identifier}")
            n.model.add_constraints(energy <= max(0.0, budget.remaining_MWh),
                                    name=f"Disruption-resource-budget-{number}")
        if budget.price_enabled and budget.scarcity_price:
            n.model.objective += budget.scarcity_price * energy


def update_resource_counters(n, implemented, budgets, window):
    rows = []
    for budget in budgets:
        supplied = n.generators_t.p.loc[implemented, list(budget.generators)]
        if supplied.lt(-1e-4).any().any():
            raise ValueError(f"Negative resource supply for {budget.identifier}")
        used = float(supplied.clip(lower=0).mul(n.snapshot_weightings.generators.loc[implemented], axis=0).to_numpy().sum())
        budget.remaining_MWh -= used
        budget.used_run_MWh += used
        if (budget.enforce or budget.allowance_MWh == 0) and budget.remaining_MWh < -1e-4:
            raise ValueError(f"Implemented dispatch exceeded {budget.identifier}")
        rows.append({"window": window, "resource": budget.identifier, "carrier": budget.carrier,
                     "allowance_MWh": budget.allowance_MWh, "used_before_run_MWh": budget.used_before_MWh,
                     "implemented_MWh": used, "used_in_run_MWh": budget.used_run_MWh,
                     "remaining_MWh": budget.remaining_MWh, "exceedance_MWh": max(0.0, -budget.remaining_MWh),
                     "enforced": budget.enforce or budget.allowance_MWh == 0,
                     "scarcity_price_EUR_per_MWh": budget.scarcity_price,
                     "scarcity_charge_EUR": used * budget.scarcity_price if budget.price_enabled else 0.0})
    return rows


def replace_co2_budget(n, reference, enabled):
    budgets = reference.global_constraints.loc[reference.global_constraints.type.eq("co2_atmosphere")]
    if not enabled or budgets.empty:
        return 0.0
    if len(budgets) != 1:
        raise ValueError("Rolling CO2 pricing requires exactly one atmospheric emissions budget.")
    row = budgets.iloc[0]
    if row.sense != "<=":
        raise ValueError("Expected an upper CO2 emissions budget.")
    price = scarcity_price(float(row.mu), budgets.index[0])
    n.remove("GlobalConstraint", budgets.index)
    return price


def add_co2_price(n, snapshots, price):
    if not price:
        return
    names = n.stores.index[n.stores.carrier.eq("co2")]
    if names.empty:
        raise ValueError("Cannot price net atmospheric emissions without an atmosphere Store.")
    # Existing operating carbon costs remain; this is the removed budget's premium.
    end = n.model["Store-e"].sel(snapshot=snapshots[-1], name=names).sum()
    initial = float(n.stores.loc[names, "e_initial"].sum())
    n.model.objective += price * (end - initial)
