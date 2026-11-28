# Shared baseline inputs are produced by the baseline campaign before arrays run.
from scripts.disruption_scenarios import select_disruption, common_policy

DISRUPTION = select_disruption(config)
config["disruption"] = DISRUPTION
DISRUPTION_DIR = DISRUPTION["output_directory"]
BASELINE_DIR = DISRUPTION["baseline_directory"]
BASE_RESULT = BASELINE_DIR + "/base_weather"
PREPARED = BASELINE_DIR + "/network_prepared.nc"
WINDOWS = BASELINE_DIR + "/event_windows.csv"
SOLVE_REPORTS = ["network_solved.nc", "resource_usage.csv", "window_summary.csv", "dispatch_summary.csv"]

rule dispatch_disruptions:
    input:
        expand(DISRUPTION_DIR + "/{case}/{report}", case=list(DISRUPTION["cases"]), report=SOLVE_REPORTS),
        timing=WINDOWS,

rule prepare_disruption_network:
    input:
        net=DISRUPTION["base_network"],
        weather=DISRUPTION.get("weather_network") or [],
        household_shares=DISRUPTION.get("household_shares") or [],
    output:
        net=PREPARED,
    params:
        load_shedding=DISRUPTION["load_shedding"],
        heat_vent=DISRUPTION["heat_vent"],
        biomass_import_capacity_MW=DISRUPTION["biomass_import_capacity_MW"],
        policy=common_policy(DISRUPTION, config["solving"], config),
        rolling_horizon=True,
        rolling=DISRUPTION["rolling_horizon"],
    script:
        "../scripts/prepare_network_disruptions.py"

rule solve_disruption_baseline:
    input:
        network=PREPARED,
        reference=DISRUPTION["base_network"],
    output:
        network=BASE_RESULT + "/network_solved.nc",
        resource_usage=BASE_RESULT + "/resource_usage.csv",
        window_summary=BASE_RESULT + "/window_summary.csv",
        dispatch_summary=BASE_RESULT + "/dispatch_summary.csv",
        storage_msv=BASELINE_DIR + "/storage_msv.csv",
        storage_msv_timeseries=BASELINE_DIR + "/storage_msv_timeseries.csv",
    log:
        solver=BASE_RESULT + "/solver.log",
        python=BASE_RESULT + "/dispatch.log",
    threads: solver_threads
    resources:
        mem_mb=config["solving"].get("mem_mb", 60000),
    params:
        solving=config["solving"],
        custom_extra_functionality=input_custom_extra_functionality,
        rolling=DISRUPTION["rolling_horizon"],
        identity={"variant": DISRUPTION["selected_variant"], "weather_year": DISRUPTION["selected_weather_year"], "case": "base_weather"},
    script:
        "../scripts/solve_disruption_dispatch.py"

rule analyse_disruption_windows:
    input:
        network=BASE_RESULT + "/network_solved.nc",
    output:
        hourly=BASELINE_DIR + "/residual_load_hourly.csv",
        windows=WINDOWS,
    params:
        region=DISRUPTION["region"],
        durations=DISRUPTION["durations_hours"],
        renewable_carriers=DISRUPTION["renewable_carriers"],
        variant=DISRUPTION["selected_variant"],
        weather_year=DISRUPTION["selected_weather_year"],
    script:
        "../scripts/analyse_disruption_windows.py"

if DISRUPTION["campaign"] != "baseline":
    rule apply_disruption:
        input:
            net=PREPARED,
            windows=WINDOWS,
        output:
            net=DISRUPTION_DIR + "/{case}/network_disrupted.nc",
            report=DISRUPTION_DIR + "/{case}/applied_disruption.csv",
        params:
            definition=lambda w: DISRUPTION["cases"][w.case],
            campaign=DISRUPTION["campaign"],
            variant=DISRUPTION["selected_variant"],
            weather_year=DISRUPTION["selected_weather_year"],
        script:
            "../scripts/apply_disruption.py"

    rule solve_disruption_dispatch:
        input:
            network=DISRUPTION_DIR + "/{case}/network_disrupted.nc",
            reference=DISRUPTION["base_network"],
        output:
            network=DISRUPTION_DIR + "/{case}/network_solved.nc",
            resource_usage=DISRUPTION_DIR + "/{case}/resource_usage.csv",
            window_summary=DISRUPTION_DIR + "/{case}/window_summary.csv",
            dispatch_summary=DISRUPTION_DIR + "/{case}/dispatch_summary.csv",
        log:
            solver=DISRUPTION_DIR + "/{case}/solver.log",
            python=DISRUPTION_DIR + "/{case}/dispatch.log",
        threads: solver_threads
        resources:
            mem_mb=config["solving"].get("mem_mb", 60000),
        params:
            solving=config["solving"],
            custom_extra_functionality=input_custom_extra_functionality,
            rolling=DISRUPTION["rolling_horizon"],
            identity=lambda w: {"variant": DISRUPTION["selected_variant"], "weather_year": DISRUPTION["selected_weather_year"], "case": w.case},
        script:
            "../scripts/solve_disruption_dispatch.py"
