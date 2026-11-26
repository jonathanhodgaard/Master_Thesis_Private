# Detailed plots are opt-in. Campaign summaries share one metrics table.
PLOTTING = DISRUPTION["plotting"]
PLOT_TYPES = ["dispatch_year", "dispatch_zoom", "demand_shares", "production_shares"]
PLOT_KINDS = ["power", "heat"]


def case_plot_file(plot, case, kind):
    return f"{DISRUPTION_DIR}/{case}/plots/{plot}_{kind}.png"


rule plot_disruptions:
    input:
        expand(DISRUPTION_DIR + "/{case}/plots/{plot}_{kind}.png",
               case=list(DISRUPTION["cases"]), plot=PLOT_TYPES, kind=PLOT_KINDS),

rule plot_outage_dispatch:
    input:
        network=DISRUPTION_DIR + "/{case}/network_solved.nc",
    output:
        **{f"{plot}_{kind}": case_plot_file(plot, "{case}", kind)
           for plot in PLOT_TYPES for kind in PLOT_KINDS},
    log:
        DISRUPTION_DIR + "/{case}/plots/dispatch.log",
    threads: 1
    resources:
        mem_mb=8000,
    params:
        plotting=PLOTTING,
    script:
        "../scripts/plot_outage_dispatch.py"

if DISRUPTION["campaign"] != "baseline":
    from scripts.disruption_scenarios import scenario_definitions
    ALL_CASES = list(scenario_definitions(DISRUPTION))
    CAMPAIGN_ROOT = DISRUPTION["output_root"] + "/" + DISRUPTION["campaign"]
    METRIC_INPUTS = [f"{CAMPAIGN_ROOT}/{variant}/{year}/{case}/{name}"
                    for variant in DISRUPTION["variants"] for year in DISRUPTION["weather_years"]
                    for case in ALL_CASES for name in ["network_solved.nc", "dispatch_summary.csv"]]
    METRIC_INPUTS += [f"{DISRUPTION['output_root']}/base_cases/{variant}/{year}/{name}"
                     for variant in DISRUPTION["variants"] for year in DISRUPTION["weather_years"]
                     for name in ["base_weather/network_solved.nc", "base_weather/dispatch_summary.csv", "event_windows.csv"]]

    rule summarise_disruptions:
        input:
            METRIC_INPUTS,
        output:
            table=CAMPAIGN_ROOT + "/plots/metrics.csv",
            **{f"{variant}_{kind}": f"{CAMPAIGN_ROOT}/plots/{variant}_{kind}.png"
               for variant in DISRUPTION["variants"] for kind in PLOT_KINDS},
        params:
            settings=DISRUPTION,
        threads: 1
        resources:
            mem_mb=16000,
        script:
            "../scripts/summarise_disruptions.py"
