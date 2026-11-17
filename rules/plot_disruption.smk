# SPDX-License-Identifier: MIT
#
# Outage plots for the disruption dispatch (scripts made from base_network_dispatch.ipynb and
# missing_demand_plots.ipynb). Included after rules/prepare_disruption.smk.
#
# Per case:   {output_directory}/{case}/plots/{dispatch_year,dispatch_zoom,demand_shares}_<label>_{power,heat}.{png,pdf}
# All cases:  {output_directory}/plots/missing_demand_<label>_{power,heat}.{png,pdf}, _summary.csv, _hourly.csv
#
# Normal run (default target dispatch_disruptions): missing networks are solved, then all
# cases are plotted. Add --rerun-triggers mtime so existing networks are not re-solved
# only because code or config changed.
#
# Plot-only run, never solves, uses only the networks that already exist:
#   snakemake plot_disruptions --config plot_existing_only=true \
#     --allowed-rules plot_outage_dispatch plot_outage_missing_demand --configfiles ...

import os

PLOTTING = DISRUPTION.get("plotting", {})
PLOT_LABEL = PLOTTING.get("region_label", "DK2")
PLOT_KINDS = ["power", "heat"]
PLOT_TYPES = ["dispatch_year", "dispatch_zoom", "demand_shares"]
PLOT_EXISTING_ONLY = bool(config.get("plot_existing_only", PLOTTING.get("existing_only", False)))


def solved_network(case):
    return DISRUPTION_DIR + f"/{case}/network_solved.nc"


def plot_cases():
    # All configured cases, or (plot-only run) only those whose solved network exists.
    cases = list(DISRUPTION["cases"])
    if PLOT_EXISTING_ONLY:
        cases = [c for c in cases if os.path.exists(solved_network(c))]
    return cases


def case_plot_files(case, ext="png"):
    return [
        DISRUPTION_DIR + f"/{case}/plots/{plot}_{PLOT_LABEL}_{kind}.{ext}"
        for plot in PLOT_TYPES
        for kind in PLOT_KINDS
    ]


MISSING_DEMAND_STEM = DISRUPTION_DIR + f"/plots/missing_demand_{PLOT_LABEL}"


def all_plot_files():
    cases = plot_cases()
    if not cases:
        return []
    files = [f for c in cases for f in case_plot_files(c)]
    return files + [MISSING_DEMAND_STEM + "_power.png", MISSING_DEMAND_STEM + "_heat.png"]


rule plot_disruptions:
    # Target for all outage plots (see header for the plot-only command).
    input:
        all_plot_files(),


rule plot_outage_dispatch:
    input:
        network=DISRUPTION_DIR + "/{case}/network_solved.nc",
    output:
        **{
            f"{plot}_{kind}": DISRUPTION_DIR + f"/{{case}}/plots/{plot}_{PLOT_LABEL}_{kind}.png"
            for plot in PLOT_TYPES
            for kind in PLOT_KINDS
        },
        pdf=[
            DISRUPTION_DIR + f"/{{case}}/plots/{plot}_{PLOT_LABEL}_{kind}.pdf"
            for plot in PLOT_TYPES
            for kind in PLOT_KINDS
        ],
    log:
        DISRUPTION_DIR + "/{case}/plots/plot_outage_dispatch.log",
    threads: 1
    resources:
        mem_mb=8000,
    params:
        plotting=PLOTTING,
    script:
        "../scripts/plot_outage_dispatch.py"


rule plot_outage_missing_demand:
    input:
        networks=lambda w: [solved_network(c) for c in plot_cases()],
    output:
        power=MISSING_DEMAND_STEM + "_power.png",
        heat=MISSING_DEMAND_STEM + "_heat.png",
        pdf=[MISSING_DEMAND_STEM + "_power.pdf", MISSING_DEMAND_STEM + "_heat.pdf"],
        summary=MISSING_DEMAND_STEM + "_summary.csv",
        hourly=MISSING_DEMAND_STEM + "_hourly.csv",
    log:
        DISRUPTION_DIR + "/plots/plot_outage_missing_demand.log",
    threads: 1
    resources:
        mem_mb=8000,
    params:
        plotting=PLOTTING,
        cases=lambda w: plot_cases(),
        all_cases=list(DISRUPTION["cases"]),
    script:
        "../scripts/plot_outage_missing_demand.py"
