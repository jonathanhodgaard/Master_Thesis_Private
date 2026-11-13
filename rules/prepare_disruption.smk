



# Custom dispatch workflow. Inputs are matching solved-base and donor networks.
DISRUPTION = config["disruption"]
DISRUPTION_DIR = DISRUPTION["output_directory"].rstrip("/")


rule dispatch_disruptions:
    input:
        expand(
            DISRUPTION_DIR + "/{case}/{report}", case=list(DISRUPTION["cases"]),
            report=["network_solved.nc", "household_heat_shares.csv", "fuel_backup_dispatch.csv",
                    "resource_usage.csv", "storage_msv.csv", "window_summary.csv", "dispatch_summary.csv"],
        )


rule prepare_disruption_network:
    input:
        net=DISRUPTION["base_network"],
        weather=lambda w: DISRUPTION["cases"][w.case].get("weather_network") or [],
        household_shares=DISRUPTION.get("household_shares") or [],
    output:
        net=DISRUPTION_DIR + "/{case}/network_prepared.nc",
    params:
        load_shedding=DISRUPTION["load_shedding"],
        fuel_backup=DISRUPTION.get("fuel_backup", {}),
        rolling_horizon=config["solving"]["options"].get("rolling_horizon", False),
        rolling=DISRUPTION.get("rolling_horizon", {}),
    script:
        "../scripts/prepare_network_disruptions.py"


rule solve_disruption_dispatch:
    input:
        network=DISRUPTION_DIR + "/{case}/network_prepared.nc",
        reference=DISRUPTION["base_network"],
    output:
        network=DISRUPTION_DIR + "/{case}/network_solved.nc",
        household_heat_shares=DISRUPTION_DIR + "/{case}/household_heat_shares.csv",
        fuel_backup_dispatch=DISRUPTION_DIR + "/{case}/fuel_backup_dispatch.csv",
        resource_usage=DISRUPTION_DIR + "/{case}/resource_usage.csv",
        storage_msv=DISRUPTION_DIR + "/{case}/storage_msv.csv",
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
        rolling=DISRUPTION.get("rolling_horizon", {}),
    script:
        "../scripts/solve_disruption_dispatch.py"


