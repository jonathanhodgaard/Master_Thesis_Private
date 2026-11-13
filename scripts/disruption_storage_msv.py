# SPDX-License-Identifier: MIT
"""Annual-average reference MSVs applied to seasonal storage flows, not endpoints."""
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


def apply_annual_msv(n, reference, options):
    """Add MSV once; return original cost inputs for separate physical-cost reporting."""
    validate_hourly_network(reference)
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
        values[component] = annual.mean()
        records.extend({"component": component, "asset": name,
                        "MSV_EUR_per_MWh_stored": float(value),
                        "reference_start": str(reference.snapshots[0]),
                        "reference_end": str(reference.snapshots[-1])}
                       for name, value in values[component].items())
    # Snapshot the unmodified costs, including replacement inventory sources.
    original = {}
    for component in ("Generator", "Link", "Store", "StorageUnit"):
        for attr in ("marginal_cost", "marginal_cost_quadratic", "marginal_cost_storage", "spill_cost"):
            if attr in n.components[component].defaults.index:
                original[component, attr] = n.get_switchable_as_dense(component, attr).copy()
    stores = values["Store"]
    if not stores.empty:
        n.stores_t.marginal_cost[stores.index] = original["Store", "marginal_cost"][stores.index].add(stores, axis=1)
    units = values["StorageUnit"]
    if not units.empty:
        efficiency = n.get_switchable_as_dense("StorageUnit", "efficiency_dispatch")[units.index]
        if (efficiency <= 0).any().any():
            raise ValueError("Selected storage units require positive discharge efficiency.")
        n.storage_units_t.marginal_cost[units.index] = original["StorageUnit", "marginal_cost"][units.index] + units / efficiency
        n.storage_units_t.spill_cost[units.index] = original["StorageUnit", "spill_cost"][units.index].add(units, axis=1)
    n.meta = {**n.meta, "disruption_msv_applied": True, "disruption_storage_msv": records}
    return values, original


def add_msv_charging_credit(n, snapshots, values):
    """StorageUnit native marginal_cost covers discharge only; credit charging."""
    units = values["StorageUnit"]
    if units.empty:
        return
    eta = n.get_switchable_as_dense("StorageUnit", "efficiency_store", snapshots)[units.index]
    coefficients = eta.mul(units, axis=1).mul(n.snapshot_weightings.objective.loc[snapshots], axis=0)
    coefficients = xr.DataArray(coefficients.to_numpy(), dims=["snapshot", "name"],
                                coords={"snapshot": snapshots, "name": units.index})
    charging = n.model["StorageUnit-p_store"].sel(snapshot=snapshots, name=units.index)
    n.model.objective -= (charging * coefficients).sum()


def initialize_storage(n, reference, start):
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


def msv_cost(n, snapshots, values):
    total = 0.0
    stores, units = values["Store"], values["StorageUnit"]
    if not stores.empty:
        total += n.stores_t.p.loc[snapshots, stores.index].mul(stores, axis=1).to_numpy().sum()
    if not units.empty:
        names = units.index
        eta_d = n.get_switchable_as_dense("StorageUnit", "efficiency_dispatch", snapshots)[names]
        eta_s = n.get_switchable_as_dense("StorageUnit", "efficiency_store", snapshots)[names]
        spill = n.storage_units_t.spill.reindex(index=snapshots, columns=names, fill_value=0).fillna(0)
        flow = n.storage_units_t.p_dispatch.loc[snapshots, names] / eta_d - n.storage_units_t.p_store.loc[snapshots, names] * eta_s + spill
        total += flow.mul(units, axis=1).to_numpy().sum()
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
