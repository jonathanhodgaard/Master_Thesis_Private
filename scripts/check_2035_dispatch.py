"""Audit solved references and construct the first production rolling window.

This is a structural preflight, not a full-year feasibility certificate. Inputs
are read-only. Use the batch campaigns to test every window and outage case.
"""
import argparse
from copy import deepcopy
import gc
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa

from scripts.run_disruption_campaign import load_config
from scripts.disruption_scenarios import select_disruption
from scripts.household_heat_constraints import freeze_household_heat_network
from scripts.prepare_network_disruptions import (
    apply_low_voltage_capacity_multipliers, set_weather, add_fuel_backup_links,
    add_load_shedding, assert_fixed_capacities,
)
from scripts.heat_vent_capacity import prepare_dispatch_heat_vent_capacity
from scripts.disruption_waste_hvc import prepare_waste_hvc
from scripts.interconnector_disruption import apply_interconnector_outages
from scripts.disruption_resource_budgets import build_resource_budgets, replace_co2_budget, add_resource_terms, add_co2_price
from scripts.disruption_storage_msv import (
    validate_hourly_network, apply_reference_msv, initialize_storage,
    storage_boundary_losses, add_msv_charging_credit,
)
from scripts.rolling_disruption_dispatch import clear_future_results


def transmission(n):
    frames = []
    for component, capacity in (("Line", "s_nom"), ("Link", "p_nom")):
        t = n.components[component].static
        ac = n.buses.index[n.buses.carrier.eq("AC")]
        t = t.loc[t.bus0.isin(ac) & t.bus1.isin(ac)]
        out = t[["bus0", "bus1", "carrier"]].copy()
        out["capacity"] = t[capacity + "_opt"]
        out["component"] = component
        frames.append(out.reset_index().set_index(["component", "name"]))
    return pd.concat(frames).sort_index()


def check(variant, year, case, build_window, native_lv, solve_window=False):
    scenario = f"sector_2035_{variant}"
    config = load_config([
        "config/config.default.yaml", f"config/config.{scenario}.yaml",
        "config/config.interconnector_disruption.yaml",
        f"config/config.interconnector_disruption_2035_{variant}.yaml",
    ])
    config.update(disruption_weather_year=year, disruption_case=case)
    d = select_disruption(config)
    config["disruption"] = d
    if native_lv:
        d["low_voltage_capacity_multipliers"] = {"FI": 1.0, "SE": 1.0}
    reference = pypsa.Network(d["base_network"])
    validate_hourly_network(reference)
    if reference.objective is None or not np.isfinite(reference.objective):
        raise ValueError("A successfully solved reference is required")
    baseline = pypsa.Network("results/sector_2025/networks/base_s_10___2025.nc")
    original = transmission(baseline)
    current = transmission(reference)
    pd.testing.assert_frame_equal(original, current, check_exact=False, rtol=1e-10)
    if not baseline.snapshots.equals(reference.snapshots):
        raise ValueError("Reference calendars differ")
    new_carriers = sorted(set(reference.links.carrier) - set(baseline.links.carrier))
    del baseline
    gc.collect()
    n = reference.copy()
    freeze_household_heat_network(n, d["household_shares"])
    donor = d["cases"][case].get("weather_network")
    if donor:
        weather = pypsa.Network(donor)
        set_weather(n, weather)
        del weather
    apply_low_voltage_capacity_multipliers(n, d["low_voltage_capacity_multipliers"])
    rolling = d["rolling_horizon"]
    prepare_waste_hvc(n, rolling["waste_hvc"])
    add_fuel_backup_links(n, d["fuel_backup"])
    prepare_dispatch_heat_vent_capacity(n, d["heat_vent"])
    add_load_shedding(n, **d["load_shedding"])
    # Validate every configured outage selector/date against the frozen network.
    all_settings = deepcopy(config)
    all_settings.pop("disruption_case", None)
    all_settings["disruption"] = load_config([
        "config/config.interconnector_disruption.yaml",
        f"config/config.interconnector_disruption_2035_{variant}.yaml",
    ])["disruption"]
    events = [event for c in select_disruption(all_settings)["cases"].values()
              for event in c.get("interconnector_outages", [])]
    apply_interconnector_outages(n.copy(), events)
    apply_interconnector_outages(n, d["cases"][case].get("interconnector_outages", []))
    assert_fixed_capacities(n)
    budgets = build_resource_budgets(n, reference, rolling["resources"], n.snapshots[0])
    price = replace_co2_budget(n, reference, rolling["co2"]["replace_budget_with_price"])
    annual = n.global_constraints.loc[n.global_constraints.type.isin(["primary_energy", "operational_limit"])]
    unsupported = annual.loc[~(annual.carrier_attribute.eq("co2 sequestered") & annual.constant.eq(0))]
    if not unsupported.empty:
        raise ValueError(f"Annual budgets without rolling adapters: {unsupported.index.tolist()}")
    values, _ = apply_reference_msv(n, reference, rolling["storage"]["msv"])
    initialize_storage(n, reference, n.snapshots[0], rolling["storage"])
    for component in ("Generator", "Link"):
        if n.components[component].static.committable.any():
            raise ValueError("Unit commitment is unsupported in rolling dispatch")
    result = {"scenario": scenario, "weather_year": year, "case": case,
              "status": "structural_preflight_passed", "solved": False,
              "transmission_equal_to_2025": True, "transmission_asset_count": len(current),
              "all_eight_outage_cases_validated": True,
              "new_link_carriers_vs_2025": new_carriers,
              "co2_reference_price_EUR_per_t": price,
              "resource_budgets": len(budgets), "seasonal_storage_duals": len(n.meta["disruption_storage_msv"]),
              "low_voltage_capacity_multipliers": d["low_voltage_capacity_multipliers"],
              "limitations": "No optimization performed; later rolling windows and donor weather require Sophia solves."}
    if build_window:
        from scripts import solve_network as solver
        clear_future_results(n, n.snapshots[0])
        window = n.snapshots[:rolling["horizon"]]
        model_kwargs, solve_kwargs = solver.collect_kwargs(config, config["solving"], [2035], mode="single")
        params = SimpleNamespace(custom_extra_functionality=None)
        with storage_boundary_losses(n, window[0]):
            solver.create_optimization_model(n, config, params, model_kwargs, solve_kwargs,
                                             planning_horizons="2035", snapshots=window)
        add_resource_terms(n, window, budgets)
        add_msv_charging_credit(n, window, values)
        add_co2_price(n, window, price)
        result.update(status="first_window_constructed", window_hours=len(window),
                      variables=n.model.nvars, constraints=n.model.ncons,
                      constraint_families=list(n.model.constraints))
        if solve_window:
            # Local smoke test only: production retains the configured Gurobi
            # options. An opening-week result cannot certify a whole campaign.
            status, condition = n.optimize.solve_model(
                solver_name="highs", assign_all_duals=False,
                solver_options={"threads": 8, "time_limit": 180,
                                "solver": "ipm", "run_crossover": "off",
                                "primal_feasibility_tolerance": 1e-7,
                                "dual_feasibility_tolerance": 1e-7,
                                "ipm_optimality_tolerance": 1e-8},
            )
            result.update(status=str(status), termination_condition=str(condition),
                          solved=status == "ok" and condition == "optimal",
                          smoke_test_solver="highs",
                          limitations="Only the opening 168-hour window tested; full-year/outage and donor feasibility still require Sophia solves.")
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--variant", choices=["w_V2G", "wo_V2G"], required=True)
    parser.add_argument("--year", default="2013-2014")
    parser.add_argument("--case", default="base_weather")
    parser.add_argument("--build-window", action="store_true")
    parser.add_argument("--solve-window", action="store_true", help="Build and solve the opening window with HiGHS (180s limit)")
    parser.add_argument("--native-low-voltage", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = check(args.variant, args.year, args.case, args.build_window or args.solve_window,
                       args.native_low_voltage, args.solve_window)
    except Exception as exc:
        result = {"scenario": f"sector_2035_{args.variant}", "weather_year": args.year,
                  "case": args.case, "status": "failed", "error": str(exc), "solved": False}
        raise
    finally:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
