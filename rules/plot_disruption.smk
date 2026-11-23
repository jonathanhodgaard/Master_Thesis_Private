# SPDX-License-Identifier: MIT
#
# Outage plots for the disruption dispatch (scripts made from base_network_dispatch.ipynb and
# missing_demand_plots.ipynb). Included after rules/prepare_disruption.smk.
#
# Figures have no titles; the file name says what they show (region, case, weather year, kind):
# Per case:   {output_directory}/{weather_year}/{case}/plots/<plot type>/
#               {dispatch_year,dispatch_zoom,demand_shares,production_shares}_<label>_<case>_<weather_year>_{power,heat}.png
# All cases:  {output_directory}/{weather_year}/plots/missing_demand/
#               missing_demand_<label>_<weather_year>_{power,heat}.png, _summary.csv, _hourly.csv
# (without disruption: weather_years there is no weather-year folder and no _<weather_year> part)
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
PLOT_TYPES = ["dispatch_year", "dispatch_zoom", "demand_shares", "production_shares"]
PLOT_EXISTING_ONLY = bool(config.get("plot_existing_only", PLOTTING.get("existing_only", False)))


def solved_network(case):
    return DISRUPTION_DIR + f"/{case}/network_solved.nc"


def plot_cases():
    # All configured cases, or (plot-only run) only those whose solved network exists.
    cases = list(DISRUPTION["cases"])
    if PLOT_EXISTING_ONLY:
        cases = [c for c in cases if os.path.exists(solved_network(c))]
    return cases


# weather-year part of the file names (set by select_disruption in prepare_disruption.smk)
PLOT_WEATHER_YEAR = DISRUPTION.get("selected_weather_year")
PLOT_WEATHER_TAG = f"_{PLOT_WEATHER_YEAR}" if PLOT_WEATHER_YEAR else ""


def case_plot_file(plot, case, kind, ext="png"):
    # case may be the literal "{case}" wildcard (rule outputs) or a case name
    return (DISRUPTION_DIR + f"/{case}/plots/{plot}/"
            f"{plot}_{PLOT_LABEL}_{case}{PLOT_WEATHER_TAG}_{kind}.{ext}")


def case_plot_files(case, ext="png"):
    return [case_plot_file(plot, case, kind, ext) for plot in PLOT_TYPES for kind in PLOT_KINDS]


MISSING_DEMAND_STEM = DISRUPTION_DIR + f"/plots/missing_demand/missing_demand_{PLOT_LABEL}{PLOT_WEATHER_TAG}"


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
            f"{plot}_{kind}": case_plot_file(plot, "{case}", kind, "png")
            for plot in PLOT_TYPES
            for kind in PLOT_KINDS
        },
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
        summary=MISSING_DEMAND_STEM + "_summary.csv",
        hourly=MISSING_DEMAND_STEM + "_hourly.csv",
    log:
        DISRUPTION_DIR + "/plots/missing_demand/plot_outage_missing_demand.log",
    threads: 1
    resources:
        mem_mb=8000,
    params:
        plotting=PLOTTING,
        cases=lambda w: plot_cases(),
        all_cases=list(DISRUPTION["cases"]),
    script:
        "../scripts/plot_outage_missing_demand.py"


# ---- resilience heatmaps across weather years (case x weather year) -------------------
# Needs all weather-year folders, so it is never part of a campaign array task. Run once
# after the array has finished, in a compute allocation (it never solves; cells without a
# solved network are grey "n/s"):
#   snakemake plot_disruption_heatmaps --cores 4 \
#     --allowed-rules disruption_case_metrics plot_disruption_heatmaps \
#     --rerun-triggers mtime --configfiles ...   (same configs as the campaign)
# Outputs: {output_directory}/plots/heatmaps/heatmap_<label>_<metric>_<baseline>.png
#            vs_same_year: outage effect alone; vs_reference: weather + outage vs the
#            no-outage case of the reference weather year (default 2013-2014)
#          {output_directory}/plots/heatmaps/resilience_metrics_<label>.csv
#          {output_directory}/{weather_year}/{case}/resilience_metrics.csv (per cell)
HEATMAP_YEARS = list(DISRUPTION.get("weather_years") or {})

if HEATMAP_YEARS:
    import re

    HEATMAP = PLOTTING.get("heatmap") or {}
    HEATMAP_REFERENCE_YEAR = HEATMAP.get("reference_weather_year") or DISRUPTION["default_weather_year"]
    HEATMAP_REFERENCE_CASE = HEATMAP.get("reference_case", "base_weather")
    # DISRUPTION_DIR ends with "/<selected weather year>" (select_disruption)
    CAMPAIGN_DIR = DISRUPTION_DIR.rsplit("/", 1)[0]
    # keep in sync with METRICS in scripts/plot_disruption_heatmaps.py; every metric is
    # drawn against both baselines (the table also has absolute values)
    HEATMAP_METRICS = ["ens_power", "price_normal", "shedding_hours",
                       "scarcity_hours_no_shedding", "consumer_cost_normal",
                       "consumer_cost_scarcity", "dispatch_cost"]
    HEATMAP_BASELINES = ["vs_same_year", "vs_reference"]
    HEATMAP_STEM = CAMPAIGN_DIR + f"/plots/heatmaps/heatmap_{PLOT_LABEL}"

    def heatmap_cells():
        # (weather year, case) pairs that have a solved network
        return [[y, c] for y in HEATMAP_YEARS for c in DISRUPTION["cases"]
                if os.path.exists(f"{CAMPAIGN_DIR}/{y}/{c}/network_solved.nc")]

    rule plot_disruption_heatmaps:
        input:
            metrics=lambda w: [f"{CAMPAIGN_DIR}/{y}/{c}/resilience_metrics.csv"
                               for y, c in heatmap_cells()],
        output:
            **{f"{m}_{b}": f"{HEATMAP_STEM}_{m}_{b}.png"
               for m in HEATMAP_METRICS for b in HEATMAP_BASELINES},
            table=CAMPAIGN_DIR + f"/plots/heatmaps/resilience_metrics_{PLOT_LABEL}.csv",
        log:
            CAMPAIGN_DIR + "/plots/heatmaps/plot_disruption_heatmaps.log",
        threads: 1
        resources:
            mem_mb=2000,
        params:
            plotting=PLOTTING,
            cells=lambda w: heatmap_cells(),
            weather_years=HEATMAP_YEARS,
            cases=list(DISRUPTION["cases"]),
            reference_year=HEATMAP_REFERENCE_YEAR,
            reference_case=HEATMAP_REFERENCE_CASE,
        script:
            "../scripts/plot_disruption_heatmaps.py"

    rule disruption_case_metrics:
        input:
            network=CAMPAIGN_DIR + "/{weather_year}/{case}/network_solved.nc",
            # optional: without it (e.g. only networks copied to a PC) the system
            # dispatch cost is NaN ("n/s"); all DK2 metrics are still computed
            summary=lambda w: [p] if os.path.exists(
                p := f"{CAMPAIGN_DIR}/{w.weather_year}/{w.case}/dispatch_summary.csv") else [],
        output:
            CAMPAIGN_DIR + "/{weather_year}/{case}/resilience_metrics.csv",
        log:
            CAMPAIGN_DIR + "/{weather_year}/{case}/resilience_metrics.log",
        wildcard_constraints:
            weather_year="|".join(re.escape(y) for y in HEATMAP_YEARS),
            case="[^/]+",
        threads: 1
        resources:
            mem_mb=8000,
        params:
            plotting=PLOTTING,
        script:
            "../scripts/plot_disruption_heatmaps.py"
