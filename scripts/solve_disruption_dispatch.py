"""Full-horizon dispatch using the existing PyPSA-Eur model constraints."""
from pathlib import Path
from contextlib import redirect_stdout
import logging

import pypsa
import pandas as pd

from scripts._helpers import configure_logging
from scripts.prepare_network_disruptions import assert_fixed_capacities
from scripts.household_heat_constraints import META_KEY, save_household_heat_shares
from scripts import solve_network as eur_solver

logger = logging.getLogger(__name__)


def save_fuel_backup_dispatch(n, csv_path):
    """Report backup use per link, labelled by fuel, country and end use."""
    rows = []
    weights = n.snapshot_weightings.generators
    for name, info in n.meta.get("fuel_backup_links", {}).items():
        flow = n.links_t.p0[name].clip(lower=0)
        delivered = (-n.links_t.p1[name]).clip(lower=0)
        rows.append({
            "backup_link": name, **info,
            "backup_capacity_MW": n.links.at[name, "p_nom"],
            "peak_input_MW": flow.max(),
            "input_MWh": flow.mul(weights).sum(),
            "delivered_MWh": delivered.mul(weights).sum(),
        })
    columns = ["backup_link", "original_link", "fuel", "end_use", "country",
               "backup_capacity_MW", "peak_input_MW", "input_MWh", "delivered_MWh"]
    pd.DataFrame(rows, columns=columns).to_csv(csv_path, index=False)


def write_infeasibility_report(n, report_path, time_limit=300):
    """Save conflicting equations and the solver IIS without changing the model."""
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        solver_model = n.model.solver_model
        # Gurobi's IIS search can be expensive on a full-year sector model.
        solver_model.setParam("TimeLimit", time_limit)
        labels = n.model.compute_infeasibilities()
        solver_model.write(str(report_path.with_suffix(".ilp")))
        with report_path.open("w", encoding="utf-8") as stream:
            stream.write(
                f"Conflicting constraint labels: {len(labels)}\n"
                f"IIS search time limit: {time_limit} seconds\n"
                "A time-limited IIS may not be minimal.\n"
                "First 100 conflicting equations (base-network date labels):\n"
            )
            with redirect_stdout(stream):
                n.model.constraints.print_labels(labels[:100])
        logger.error("Infeasibility diagnostics saved to %s and .ilp", report_path)
    except Exception as exc:
        # Preserve the original solve failure even if diagnostics are unavailable.
        report_path.write_text(f"Could not compute infeasibility diagnostics: {exc}\n", encoding="utf-8")
        logger.exception("Could not compute infeasibility diagnostics")


if __name__ == "__main__":
    configure_logging(snakemake)
    n = pypsa.Network(snakemake.input.network)
    assert_fixed_capacities(n)
    if META_KEY in n.meta and n.meta[META_KEY].get("mode") != "dispatch":
        raise ValueError("Household shares must be fixed before dispatch.")
    if snakemake.params.solving["options"].get("rolling_horizon", False):
        raise ValueError("This rule solves full-horizon dispatch; disable rolling_horizon.")
    # Reuse constraints without rerunning investment/topology preparation.
    eur_solver.snakemake = snakemake
    horizon = str(snakemake.config["disruption"]["planning_horizon"])
    model_kwargs, solve_kwargs = eur_solver.collect_kwargs(
        snakemake.config, snakemake.params.solving, [int(horizon)],
        log_fn=snakemake.log.solver, mode="single",
    )
    if solve_kwargs["solver_name"] == "gurobi":
        # Otherwise presolve may report only the ambiguous INF_OR_UNBD status.
        solve_kwargs["solver_options"] = dict(solve_kwargs["solver_options"])
        solve_kwargs["solver_options"]["DualReductions"] = 0
    eur_solver.create_optimization_model(
        n, snakemake.config, snakemake.params,
        model_kwargs, solve_kwargs, planning_horizons=horizon,
    )
    status, condition = n.optimize.solve_model(**solve_kwargs)
    if status != "ok" or condition != "optimal":
        report = Path(snakemake.output.network).with_name("infeasibility.txt")
        if condition == "infeasible" and solve_kwargs["solver_name"] == "gurobi":
            write_infeasibility_report(
                n, report,
                snakemake.config["disruption"].get("iis_time_limit", 300),
            )
        raise RuntimeError(f"Dispatch did not solve optimally: {status}, {condition}")
    assert_fixed_capacities(n)
    Path(snakemake.output.network).parent.mkdir(parents=True, exist_ok=True)
    if META_KEY in n.meta:
        save_household_heat_shares(n, snakemake.output.household_heat_shares)
    else:
        Path(snakemake.output.household_heat_shares).write_text(
            "group,share,heat_bus,technology\n", encoding="utf-8",
        )
    save_fuel_backup_dispatch(n, snakemake.output.fuel_backup_dispatch)
    n.export_to_netcdf(snakemake.output.network)
