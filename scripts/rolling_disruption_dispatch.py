# SPDX-License-Identifier: MIT
"""Fixed-capacity rolling dispatch with committed-only accounting and MSV bids."""
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.disruption_resource_budgets import (
    build_resource_budgets, add_resource_terms, update_resource_counters,
    replace_co2_budget, add_co2_price,
)
from scripts.disruption_storage_msv import (
    validate_hourly_network, apply_annual_msv, add_msv_charging_credit,
    initialize_storage, carry_storage, msv_cost, operating_cost,
)
from scripts.prepare_network_disruptions import assert_fixed_capacities

RESOURCE_COLUMNS = ["window", "resource", "carrier", "allowance_MWh", "used_before_run_MWh",
                    "implemented_MWh", "used_in_run_MWh", "remaining_MWh", "exceedance_MWh",
                    "enforced", "scarcity_price_EUR_per_MWh", "scarcity_charge_EUR"]
MSV_COLUMNS = ["component", "asset", "MSV_EUR_per_MWh_stored", "reference_start", "reference_end"]


def output_frames(n):
    for component in sorted(n.all_components):
        c = n.components[component]
        inputs = set(c.defaults.index[c.defaults.status.str.startswith("Input")])
        for attr, frame in list(c.dynamic.items()):
            if attr not in inputs:
                yield component, attr, frame


def clear_future_results(n, start):
    """Retain the reference prefix for ramp constraints; never expose future solutions."""
    for component, attr, frame in list(output_frames(n)):
        cleared = pd.DataFrame(np.nan, index=n.snapshots, columns=frame.columns)
        prefix = frame.index[frame.index < start]
        cleared.loc[prefix] = frame.loc[prefix]
        n.components[component].dynamic[attr] = cleared


def simulation_period(n, options):
    settings = options.get("simulation", {})
    start = pd.Timestamp(settings.get("start") or n.snapshots[0])
    end = pd.Timestamp(settings.get("end") or n.snapshots[-1] + pd.Timedelta(hours=1))
    if start not in n.snapshots or end > n.snapshots[-1] + pd.Timedelta(hours=1):
        raise ValueError("Simulation bounds must lie within the reference accounting year.")
    if end != end.floor("h"):
        raise ValueError("Simulation end must be aligned with hourly snapshots.")
    snapshots = n.snapshots[(n.snapshots >= start) & (n.snapshots < end)]
    if snapshots.empty:
        raise ValueError("The requested simulation period is empty.")
    return snapshots


def solve_rolling_dispatch(n, reference, config, params, model_kwargs, solve_kwargs,
                           planning_horizon, options, output_directory=None,
                           failure_callback=None):
    # Import lazily: the helper remains importable without the full solver stack.
    from scripts import solve_network as eur_solver

    validate_hourly_network(n)
    validate_hourly_network(reference)
    if reference.objective is None or not np.isfinite(reference.objective):
        raise ValueError("Storage initialization and prices require a successfully solved reference network.")
    if not reference.snapshots.equals(n.snapshots):
        raise ValueError("Prepared and reference networks must have the same calendar snapshots.")
    assert_fixed_capacities(n)
    horizon, overlap = options.get("horizon", 48), options.get("overlap", 24)
    if not isinstance(horizon, int) or not isinstance(overlap, int) or not 0 <= overlap < horizon:
        raise ValueError("Require integer snapshot lengths with 0 <= overlap < horizon.")
    if options.get("storage", {}).get("initial_state", "base_previous_snapshot") != "base_previous_snapshot":
        raise ValueError("Supported initial_state is base_previous_snapshot.")
    msv_options = options.get("storage", {}).get("msv", {})
    if msv_options.get("aggregation", "annual_mean") != "annual_mean" or msv_options.get("source", "mu_energy_balance") != "mu_energy_balance":
        raise ValueError("Supported MSV source is annual_mean of mu_energy_balance.")
    for component in ("Generator", "Link"):
        table = n.components[component].static
        if "committable" in table and table.committable.any():
            raise ValueError("This rolling implementation currently supports continuous dispatch, not unit commitment.")
    imports = config.get("sector", {}).get("imports", {})
    if imports.get("enable", False) and np.isfinite(imports.get("limit", np.inf)):
        raise ValueError("Finite aggregate green-import budgets need an explicit ledger adapter before rolling dispatch.")
    snapshots = simulation_period(n, options)
    # Work on the prepared copy only; reference inputs and original base files are read-only.
    budgets = build_resource_budgets(n, reference, options.get("resources", {}), snapshots[0])
    co2_price = replace_co2_budget(n, reference, options.get("co2", {}).get("replace_budget_with_price", True))
    annual = n.global_constraints.loc[n.global_constraints.type.isin(["primary_energy", "operational_limit"])]
    # A zero sequestration allowance is a prohibition and can remain unchanged.
    unsupported = annual.loc[~(annual.carrier_attribute.eq("co2 sequestered") & annual.constant.eq(0))]
    if not unsupported.empty:
        raise ValueError(f"Annual global budgets without a rolling adapter: {unsupported.index.tolist()}")
    values, original_costs = apply_annual_msv(n, reference, msv_options)
    initialize_storage(n, reference, snapshots[0])
    clear_future_results(n, snapshots[0])
    initial_states = {component: n.components[component].static[attr].copy()
                      for component, attr in (("Store", "e_initial"), ("StorageUnit", "state_of_charge_initial"))}
    atmosphere = n.stores.index[n.stores.carrier.eq("co2")]
    resource_rows, windows, committed = [], [], {}
    step = horizon - overlap
    for number, start in enumerate(range(0, len(snapshots), step)):
        window = snapshots[start:start + horizon]
        implemented = window[:step]
        initial_co2 = float(n.stores.loc[atmosphere, "e_initial"].sum())
        eur_solver.create_optimization_model(
            n, deepcopy(config), params, model_kwargs, solve_kwargs,
            planning_horizons=str(planning_horizon), snapshots=window,
        )
        add_resource_terms(n, window, budgets)
        add_msv_charging_credit(n, window, values)
        add_co2_price(n, window, co2_price)
        kwargs = deepcopy(solve_kwargs)
        if output_directory is not None:
            directory = Path(output_directory)
            directory.mkdir(parents=True, exist_ok=True)
            kwargs["log_fn"] = str(directory / f"solver_window_{number:04d}.log")
        status, condition = n.optimize.solve_model(**kwargs)
        if status != "ok" or condition != "optimal":
            if failure_callback is not None:
                failure_callback(n, number, status, condition)
            raise RuntimeError(f"Rolling window {number} did not solve optimally: {status}, {condition}")
        rows = update_resource_counters(n, implemented, budgets, number)
        resource_rows.extend(rows)
        emissions = float(n.stores_t.e.loc[implemented[-1], atmosphere].sum() - initial_co2)
        cost = operating_cost(n, implemented, original_costs)
        storage_charge = msv_cost(n, implemented, values)
        resource_charge = sum(row["scarcity_charge_EUR"] for row in rows)
        windows.append({"window": number, "optimization_start": str(window[0]),
                        "optimization_end": str(window[-1]), "implemented_start": str(implemented[0]),
                        "implemented_end": str(implemented[-1]), "implemented_hours": len(implemented),
                        "window_objective_EUR": float(n.model.objective.value),
                        "operating_cost_EUR": cost, "MSV_charge_EUR": storage_charge,
                        "resource_charge_EUR": resource_charge,
                        "net_emissions_tCO2": emissions, "CO2_premium_EUR": emissions * co2_price})
        for component, attr, frame in output_frames(n):
            if not frame.empty:
                committed.setdefault((component, attr), []).append(frame.loc[implemented].copy())
        carry_storage(n, implemented[-1])
        assert_fixed_capacities(n)
    n.set_snapshots(snapshots)
    for (component, attr), frames in committed.items():
        n.components[component].dynamic[attr] = pd.concat(frames).reindex(snapshots)
    # Save the actual run's initial conditions, not the initial state of the last window.
    for component, attr in (("Store", "e_initial"), ("StorageUnit", "state_of_charge_initial")):
        n.components[component].static[attr] = initial_states[component]
    window_table = pd.DataFrame(windows)
    totals = window_table[["operating_cost_EUR", "MSV_charge_EUR", "resource_charge_EUR", "net_emissions_tCO2", "CO2_premium_EUR"]].sum().to_dict()
    totals["priced_dispatch_cost_EUR"] = sum(totals[k] for k in ("operating_cost_EUR", "MSV_charge_EUR", "resource_charge_EUR", "CO2_premium_EUR"))
    totals["implemented_hours"] = len(snapshots)
    n._objective = totals["priced_dispatch_cost_EUR"]
    n._objective_constant = 0.0
    n.meta = {**n.meta, "rolling_dispatch": {"horizon": horizon, "overlap": overlap,
              "start": str(snapshots[0]), "end": str(snapshots[-1]),
              "resource_allowances_enforced": options.get("resources", {}).get("enforce_allowances", False),
              "separate_terminal_value": False, "summary": totals}}
    return {
        "resource_usage": pd.DataFrame(resource_rows, columns=RESOURCE_COLUMNS),
        "storage_msv": pd.DataFrame(n.meta["disruption_storage_msv"], columns=MSV_COLUMNS),
        "window_summary": window_table,
        "dispatch_summary": pd.DataFrame([totals]),
    }
