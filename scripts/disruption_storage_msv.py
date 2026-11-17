# SPDX-License-Identifier: MIT
"""Reference MSVs applied to seasonal storage flows, not endpoints."""
from contextlib import contextmanager

import numpy as np
import pandas as pd
import xarray as xr

ACCOUNTING_CARRIERS = {"co2", "co2 sequestered", "non-sequestered HVC", "solid biomass import"}


def validate_hourly_network(n):
    if not isinstance(n.snapshots, pd.DatetimeIndex) or n.snapshots.empty:
        raise ValueError("Rolling dispatch requires single-period datetime snapshots.")
    if len(n.snapshots) > 1 and not (n.snapshots.to_series().diff().dropna() == pd.Timedelta(hours=1)).all():
        raise ValueError("Rolling dispatch currently requires consecutive hourly snapshots.")
    if not np.allclose(n.snapshot_weightings.to_numpy(), 1):
        raise ValueError("Rolling dispatch currently requires unit hourly snapshot weights.")


def selected_storage(n, component, settings):
    table = n.components[component].static
    explicit = pd.Index(settings.get("assets", []))
    missing = explicit.difference(table.index)
    if len(missing):
        raise ValueError(f"Unknown seasonal {component} assets: {missing.tolist()}")
    names = table.index[table.carrier.isin(settings.get("carriers", []))].union(explicit)
    if table.loc[names, "carrier"].isin(ACCOUNTING_CARRIERS).any():
        raise ValueError("Resource inventories and accounting stores cannot receive seasonal MSV costs.")
    capacity = table.e_nom if component == "Store" else table.p_nom * table.max_hours
    return names.intersection(table.index[capacity > 0])


def msv_profile(values, snapshots):
    """Align hourly values or broadcast an annual mean to the requested hours."""
    if isinstance(values, pd.DataFrame):
        return values.loc[snapshots]
    return pd.DataFrame(np.tile(values.to_numpy(), (len(snapshots), 1)),
                        index=snapshots, columns=values.index)


def apply_reference_msv(n, reference, options):
    """Add MSV once; return original cost inputs for separate physical-cost reporting."""
    validate_hourly_network(reference)
    aggregation = options.get("aggregation", "annual_mean")
    if aggregation not in {"annual_mean", "hourly"}:
        raise ValueError("MSV aggregation must be annual_mean or hourly.")
    overrides = options.get("aggregation_by_carrier", {})
    if not isinstance(overrides, dict) or any(v not in {"annual_mean", "hourly"} for v in overrides.values()):
        raise ValueError("MSV aggregation_by_carrier must map carriers to annual_mean or hourly.")
    if options.get("source", "mu_energy_balance") != "mu_energy_balance":
        raise ValueError("Supported MSV source is mu_energy_balance.")
    if len(n.snapshots.difference(reference.snapshots)):
        raise ValueError("Missing reference hours for storage MSVs.")
    if n.meta.get("disruption_msv_applied", False):
        raise ValueError("MSV costs have already been applied to this network.")
    values, records = {}, []
    for component, key in (("Store", "stores"), ("StorageUnit", "storage_units")):
        names = selected_storage(n, component, options.get(key, {})) if options.get("enable", True) else pd.Index([])
        if names.empty:
            values[component] = pd.Series(dtype=float)
            continue
        duals = reference.components[component].dynamic.get("mu_energy_balance")
        if duals is None or len(names.difference(duals.columns)):
            raise ValueError(f"Missing reference {component} storage-balance duals.")
        annual = duals.loc[reference.snapshots, names]
        if not np.isfinite(annual.to_numpy()).all():
            raise ValueError(f"Nonfinite reference {component} storage-balance duals.")
        modes = n.components[component].static.loc[names, "carrier"].map(overrides).fillna(aggregation)
        if modes.eq("annual_mean").all():
            values[component] = annual.mean()
        else:
            profile = annual.loc[n.snapshots].copy()
            averaged = names[modes.eq("annual_mean")]
            for name in averaged:
                profile[name] = annual[name].mean()
            values[component] = profile
        records.extend({"component": component, "asset": name,
                        "MSV_EUR_per_MWh_stored": float(value),
                        "reference_start": str(reference.snapshots[0]),
                        "reference_end": str(reference.snapshots[-1]),
                        "aggregation": modes[name],
                        "minimum_MSV_EUR_per_MWh_stored": float(annual[name].min()) if modes[name] == "hourly" else float(value),
                        "maximum_MSV_EUR_per_MWh_stored": float(annual[name].max()) if modes[name] == "hourly" else float(value)}
                       for name, value in annual.mean().items())
    # Snapshot the unmodified costs, including replacement inventory sources.
    original = {}
    for component in ("Generator", "Link", "Store", "StorageUnit"):
        for attr in ("marginal_cost", "marginal_cost_quadratic", "marginal_cost_storage", "spill_cost"):
            if attr in n.components[component].defaults.index:
                original[component, attr] = n.get_switchable_as_dense(component, attr).copy()
    stores = msv_profile(values["Store"], n.snapshots)
    if not stores.empty:
        n.stores_t.marginal_cost[stores.columns] = original["Store", "marginal_cost"][stores.columns] + stores
    units = msv_profile(values["StorageUnit"], n.snapshots)
    if not units.empty:
        efficiency = n.get_switchable_as_dense("StorageUnit", "efficiency_dispatch")[units.columns]
        if (efficiency <= 0).any().any():
            raise ValueError("Selected storage units require positive discharge efficiency.")
        n.storage_units_t.marginal_cost[units.columns] = original["StorageUnit", "marginal_cost"][units.columns] + units / efficiency
        n.storage_units_t.spill_cost[units.columns] = original["StorageUnit", "spill_cost"][units.columns] + units
    n.meta = {**n.meta, "disruption_msv_applied": True, "disruption_storage_msv": records,
              "disruption_msv_aggregation": aggregation,
              "disruption_msv_aggregation_by_carrier": dict(overrides)}
    return values, original


# Retain compatibility with callers using the original annual-only helper.
apply_annual_msv = apply_reference_msv


def add_msv_charging_credit(n, snapshots, values):
    """StorageUnit native marginal_cost covers discharge only; credit charging."""
    units = msv_profile(values["StorageUnit"], snapshots)
    if units.empty:
        return
    eta = n.get_switchable_as_dense("StorageUnit", "efficiency_store", snapshots)[units.columns]
    coefficients = (eta * units).mul(n.snapshot_weightings.objective.loc[snapshots], axis=0)
    coefficients = xr.DataArray(coefficients.to_numpy(), dims=["snapshot", "name"],
                                coords={"snapshot": snapshots, "name": units.columns})
    charging = n.model["StorageUnit-p_store"].sel(snapshot=snapshots, name=units.columns)
    n.model.objective -= (charging * coefficients).sum()


def apply_initial_inventory(n, reference, start, rules):
    """Set explicitly sourced starting inventories, never recurring minimum stocks.

    Fractions use frozen Store capacity. A common fraction at several facilities
    is a regional allocation assumption, not facility-level observed inventory.
    Interior starts need a carried rolling state, not a fresh July endowment.
    """
    if not rules:
        return
    if start != reference.snapshots[0]:
        raise ValueError("Initial inventory overrides require a simulation starting at the reference start; use carried rolling stocks for interior starts.")
    records, selected = [], set()
    for rule in rules:
        fraction = rule["fill_fraction"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not np.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("Initial inventory fill_fraction must be finite and between zero and one.")
        basis = rule.get("basis", "")
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError("Initial inventory overrides require a documented basis.")
        if rule.get("start_month_day") != start.strftime("%m-%d"):
            raise ValueError("Initial inventory date does not match the configured start_month_day.")
        countries = rule.get("countries", [])
        carriers = rule.get("carriers", [])
        assets = rule.get("assets", [])
        if not isinstance(assets, list) or any(not isinstance(name, str) for name in assets):
            raise ValueError("Initial inventory assets must be a list of exact Store names.")
        if not carriers or not (countries or assets):
            raise ValueError("Initial inventory overrides require carriers and countries or assets.")
        if set(carriers).intersection(ACCOUNTING_CARRIERS):
            raise ValueError("Physical initial inventory overrides cannot target accounting stores.")
        stores = n.stores
        country_codes = n.buses.get("country", pd.Series("", index=n.buses.index, dtype=object))
        country = stores.bus.map(country_codes).fillna("")
        names = stores.index[stores.carrier.isin(carriers) & country.isin(countries)]
        explicit = pd.Index(assets)
        missing = explicit.difference(stores.index)
        if len(missing):
            raise ValueError(f"Unknown initial inventory Store assets: {missing.tolist()}")
        if not stores.loc[explicit, "carrier"].isin(carriers).all():
            raise ValueError("Initial inventory assets must match the selected carriers.")
        names = names.union(explicit)
        if names.empty:
            raise ValueError("Initial inventory override matched no stores.")
        for name in names:
            if name in selected:
                raise ValueError(f"Overlapping initial inventory overrides: {name}")
            row = stores.loc[name]
            if row.e_nom_extendable or not np.isfinite(row.e_nom) or row.e_nom < 0:
                raise ValueError(f"Initial inventories require finite fixed storage capacities: {name}")
            selected.add(name)
            records.append({"asset": name, "country": str(country[name]),
                            "carrier": str(row.carrier), "capacity_MWh": float(row.e_nom),
                            "previous_initial_MWh": float(row.e_initial),
                            "initial_MWh": float(fraction * row.e_nom),
                            "fill_fraction": float(fraction), "start": str(start),
                            "basis": basis})
    # Validate all selections before mutating the inventory.
    for record in records:
        n.stores.at[record["asset"], "e_initial"] = record["initial_MWh"]
    n.meta = {**n.meta, "initial_inventory_overrides": records}


def initialize_storage(n, reference, start, options=None):
    """Use the predecessor state; wrap only originally cyclic reference assets."""
    position = reference.snapshots.get_loc(start)
    hvc = set(n.meta.get("rolling_waste_hvc", {}).get("stores", []))
    for component, state, initial, cyclic in (
        ("Store", "e", "e_initial", "e_cyclic"),
        ("StorageUnit", "state_of_charge", "state_of_charge_initial", "cyclic_state_of_charge"),
    ):
        table = n.components[component].static
        ref = reference.components[component]
        for name in table.index.intersection(ref.static.index):
            if name in hvc:
                continue
            if position == 0 and not ref.static.at[name, cyclic]:
                value = ref.static.at[name, initial]
            else:
                frame = ref.dynamic[state]
                # PyPSA omits all-default (zero) output columns from NetCDF.
                # The driver separately requires a solved reference network.
                value = (frame.at[reference.snapshots[position - 1], name]
                         if name in frame.columns else 0.0)
            if not np.isfinite(value):
                raise ValueError(f"Nonfinite initial state for {component} {name}.")
            table.at[name, initial] = value
        table[cyclic] = False
    apply_initial_inventory(n, reference, start, (options or {}).get("initial_inventory", []))


def carry_storage(n, snapshot):
    for component, state, initial in (
        ("Store", "e", "e_initial"),
        ("StorageUnit", "state_of_charge", "state_of_charge_initial"),
    ):
        c = n.components[component]
        if c.static.empty:
            continue
        values = c.dynamic[state].loc[snapshot].reindex(c.static.index)
        if not np.isfinite(values.to_numpy()).all():
            raise ValueError(f"Missing solved {component} states at {snapshot}.")
        c.static[initial] = values


@contextmanager
def storage_boundary_losses(n, snapshot):
    """Pass post-loss initial stocks to PyPSA, retaining physical states outside model creation.

    Native noncyclic storage balances use initial stocks without standing losses
    at the first snapshot. Apply that snapshot's retention factor exactly once;
    PyPSA handles losses for the remaining snapshots inside the window.
    """
    originals = {}
    try:
        hours = n.snapshot_weightings.at[snapshot, "stores"]
        for component, initial in (("Store", "e_initial"),
                                   ("StorageUnit", "state_of_charge_initial")):
            table = n.components[component].static
            if table.empty:
                continue
            loss = n.get_switchable_as_dense(component, "standing_loss", pd.Index([snapshot])).loc[snapshot]
            if not np.isfinite(loss.to_numpy()).all() or not loss.between(0, 1).all():
                raise ValueError(f"Invalid {component} standing losses at {snapshot}.")
            originals[component, initial] = table[initial].copy()
            table[initial] = originals[component, initial] * (1 - loss) ** hours
        yield
    finally:
        for (component, initial), values in originals.items():
            n.components[component].static[initial] = values


def msv_cost(n, snapshots, values):
    total = 0.0
    stores, units = (msv_profile(values[c], snapshots) for c in ("Store", "StorageUnit"))
    if not stores.empty:
        total += (n.stores_t.p.loc[snapshots, stores.columns] * stores).to_numpy().sum()
    if not units.empty:
        names = units.columns
        eta_d = n.get_switchable_as_dense("StorageUnit", "efficiency_dispatch", snapshots)[names]
        eta_s = n.get_switchable_as_dense("StorageUnit", "efficiency_store", snapshots)[names]
        spill = n.storage_units_t.spill.reindex(index=snapshots, columns=names, fill_value=0).fillna(0)
        flow = n.storage_units_t.p_dispatch.loc[snapshots, names] / eta_d - n.storage_units_t.p_store.loc[snapshots, names] * eta_s + spill
        total += (flow * units).to_numpy().sum()
    return float(total)


def operating_cost(n, snapshots, original):
    """Variable operating costs, excluding MSV and added budget premiums."""
    variables = {"Generator": "p", "Link": "p0", "Store": "p", "StorageUnit": "p_dispatch"}
    total = 0.0
    for (component, attr), costs in original.items():
        c = n.components[component]
        state = variables[component]
        if attr == "marginal_cost_storage":
            state = "e" if component == "Store" else "state_of_charge"
        elif attr == "spill_cost":
            state = "spill"
        frame = c.dynamic.get(state)
        if frame is None or frame.empty:
            continue
        flow = frame.reindex(index=snapshots, columns=costs.columns, fill_value=0).fillna(0)
        if attr == "marginal_cost_quadratic":
            flow = flow ** 2
        total += (flow * costs.loc[snapshots]).to_numpy().sum()
    return float(total)
