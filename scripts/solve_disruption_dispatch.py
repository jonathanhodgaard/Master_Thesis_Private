"""Full-horizon dispatch using the existing PyPSA-Eur model constraints."""
from pathlib import Path

import pypsa

from scripts._helpers import configure_logging
from scripts.prepare_network_disruptions import assert_fixed_capacities
from scripts.household_heat_constraints import META_KEY, save_household_heat_shares
from scripts import solve_network as eur_solver


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
    eur_solver.create_optimization_model(
        n, snakemake.config, snakemake.params,
        model_kwargs, solve_kwargs, planning_horizons=horizon,
    )
    status, condition = n.optimize.solve_model(**solve_kwargs)
    if status != "ok" or condition != "optimal":
        raise RuntimeError(f"Dispatch did not solve optimally: {status}, {condition}")
    assert_fixed_capacities(n)
    Path(snakemake.output.network).parent.mkdir(parents=True, exist_ok=True)
    if META_KEY in n.meta:
        save_household_heat_shares(n, snakemake.output.household_heat_shares)
    else:
        Path(snakemake.output.household_heat_shares).write_text(
            "group,share,heat_bus,technology\n", encoding="utf-8",
        )
    n.export_to_netcdf(snakemake.output.network)
