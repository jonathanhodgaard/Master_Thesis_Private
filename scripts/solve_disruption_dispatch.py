"""Solve fixed-capacity dispatch, retaining compact reports and failure evidence."""
from contextlib import redirect_stdout
from pathlib import Path
import logging
import os
import subprocess

import pandas as pd
import pypsa

from scripts._helpers import configure_logging
from scripts.prepare_network_disruptions import assert_fixed_capacities
from scripts.household_heat_constraints import META_KEY
from scripts.fuel_demand_shedding import fuel_shedding_reports
from scripts.hydrogen_balance_shedding import (
    add_hydrogen_shedding_constraints, hydrogen_shedding_reports,
)
from scripts.disruption_diagnostics import RollingDiagnostics, close_solver_log
from scripts.disruption_scenarios import common_policy

logger = logging.getLogger(__name__)


def write_infeasibility_report(n, report_path, time_limit=60, save_failed_model=False):
    """One readable conflict report, including bounds; optional single model export."""
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        solver = n.model.solver_model
        if save_failed_model:
            solver.write(str(report_path.with_suffix(".mps.gz")))
        solver.setParam("TimeLimit", time_limit)
        labels = n.model.compute_infeasibilities()
        conflict = report_path.with_suffix(".ilp")
        solver.write(str(conflict))
        with report_path.open("w", encoding="utf-8") as stream:
            stream.write(f"IIS time limit: {time_limit} s\nIIS minimal: {bool(solver.IISMinimal)}\n")
            stream.write("An IIS is one conflicting subsystem, not a unique root cause.\n\nNamed model constraints:\n")
            with redirect_stdout(stream):
                n.model.constraints.print_labels(labels)
            stream.write("\nSolver conflict, including variable bounds (text may round coefficients):\n")
            stream.write(conflict.read_text(encoding="utf-8"))
        conflict.unlink()
    except Exception as exc:
        with report_path.open("a", encoding="utf-8") as stream:
            stream.write(f"\nConflict search incomplete or unavailable: {exc}\n")
        logger.exception("Could not complete infeasibility diagnostics")


def revision():
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        diff = subprocess.check_output(["git", "status", "--porcelain"], text=True)
        return {"commit": head, "working_tree_modified": bool(diff)}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None}


def run(smk):
    from scripts import solve_network as eur_solver
    from scripts.rolling_disruption_dispatch import solve_rolling_dispatch

    configure_logging(smk)
    directory = Path(smk.output.network).parent
    settings = smk.config["disruption"]
    info = {"identity": dict(smk.params.identity), "inputs": dict(smk.input.items()),
            "reference_policy": "Original solved infrastructure reference supplies opening states, MSVs and scarcity prices; weather donor supplies operating inputs only.",
            "rolling_horizon": dict(smk.params.rolling), "load_shedding": settings["load_shedding"],
            "biomass_import_capacity_MW": settings["biomass_import_capacity_MW"],
            "common_policy": common_policy(settings, dict(smk.params.solving), smk.config),
            "solving": dict(smk.params.solving), "code": revision(),
            "slurm": {key: os.environ.get(key) for key in ("SLURM_JOB_ID", "SLURM_ARRAY_TASK_ID")}}
    diagnostics = RollingDiagnostics(directory, info)
    n = None
    try:
        n = pypsa.Network(smk.input.network)
        reference = pypsa.Network(smk.input.reference)
        if n.meta.get("disruption_preparation", {}).get("policy") != info["common_policy"]:
            raise ValueError("Prepared network policy differs from requested settings; rerun the baseline campaign first.")
        info["event"] = n.meta.get("disruption_event")
        from scripts.disruption_diagnostics import write_json
        write_json(directory / "run_info.json", info)
        assert_fixed_capacities(n)
        if META_KEY in n.meta and n.meta[META_KEY].get("mode") != "dispatch":
            raise ValueError("Household shares must be fixed before dispatch.")
        eur_solver.snakemake = smk
        horizon = str(settings["planning_horizon"])
        model_kwargs, solve_kwargs = eur_solver.collect_kwargs(
            smk.config, smk.params.solving, [int(horizon)], log_fn=smk.log.solver, mode="single")
        if solve_kwargs["solver_name"] == "gurobi":
            solve_kwargs["solver_options"] = dict(solve_kwargs["solver_options"])
            solve_kwargs["solver_options"]["DualReductions"] = 0

        def on_failure(network, number, status, condition):
            if condition == "infeasible" and solve_kwargs["solver_name"] == "gurobi":
                write_infeasibility_report(
                    network, diagnostics.failure_directory / "infeasibility.txt",
                    settings.get("iis_time_limit", 60), settings.get("save_failed_model", False))

        if smk.params.solving["options"].get("rolling_horizon", False):
            reports = solve_rolling_dispatch(
                n, reference, smk.config, smk.params, model_kwargs, solve_kwargs, horizon,
                smk.params.rolling, output_directory=directory, failure_callback=on_failure,
                diagnostics=diagnostics, export_msv=bool(smk.output.get("storage_msv")))
        else:
            diagnostics.phase("building_model")
            eur_solver.create_optimization_model(n, smk.config, smk.params, model_kwargs, solve_kwargs,
                                                planning_horizons=horizon)
            add_hydrogen_shedding_constraints(n, n.snapshots)
            diagnostics.phase("solving")
            try:
                status, condition = n.optimize.solve_model(**solve_kwargs)
            finally:
                close_solver_log(n, solve_kwargs["solver_name"])
            if status != "ok" or condition != "optimal":
                diagnostics.phase("solve_failed", solver_status=str(status), termination_condition=str(condition))
                on_failure(n, 0, status, condition)
                raise RuntimeError(f"Dispatch did not solve optimally: {status}, {condition}")
            reports = {"resource_usage": pd.DataFrame(columns=["resource", "remaining_MWh"]),
                       "window_summary": pd.DataFrame(columns=["window", "window_objective_EUR"]),
                       "dispatch_summary": pd.DataFrame([{"mode": "full_horizon", "objective_EUR": n.objective}])}
        diagnostics.phase("saving_results")
        # Compute totals without exporting duplicate hourly fuel/H2 tables.
        fuel = fuel_shedding_reports(n)["fuel_shedding_by_demand"]
        hydrogen = hydrogen_shedding_reports(n)["hydrogen_shedding_by_bus"]
        totals = {"fuel_unserved_MWh": float(fuel.unserved_MWh.sum()),
                  "fuel_shedding_cost_EUR": float(fuel.penalty_cost_EUR.sum()),
                  "hydrogen_supply_shortfall_MWh": float(hydrogen.missing_hydrogen_MWh.sum()),
                  "hydrogen_shedding_cost_EUR": float(hydrogen.penalty_cost_EUR.sum())}
        for key, value in totals.items():
            reports["dispatch_summary"][key] = value
        if "rolling_dispatch" in n.meta:
            n.meta["rolling_dispatch"]["summary"].update(totals)
        for name, frame in reports.items():
            if smk.output.get(name):
                frame.to_csv(smk.output[name], index=False)
        assert_fixed_capacities(n)
        n.export_to_netcdf(smk.output.network)
        diagnostics.phase("complete")
    except Exception as exc:
        if diagnostics.current.get("phase") != "failed":
            diagnostics.failure(exc, n)
        raise


if __name__ == "__main__":
    run(snakemake)
