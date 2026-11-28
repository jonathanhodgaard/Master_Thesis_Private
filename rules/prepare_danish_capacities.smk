# Danish source snapshots -> model input tables. Shared across scenarios/weather years.
DANISH_SOURCE = "data/danish_capacity_sources/"
DANISH_CAPACITIES = "data/existing_infrastructure/"

rule prepare_danish_capacities:
    input:
        DANISH_CAPACITIES + "powerplants.csv",
        DANISH_CAPACITIES + "existing_solar_rooftop_capacities.csv",
        DANISH_CAPACITIES + "existing_chp_heat_capacities.csv",
        DANISH_CAPACITIES + "existing_rural_decentral_heat_capacities.csv",

rule prepare_danish_power_capacities:
    input:
        ens=DANISH_SOURCE + "ENS_power_plant_register.csv",
        wind_solar=DANISH_SOURCE + "ENS_wind_&_solar_register.csv",
        ppm=DANISH_SOURCE + "powerplants_PPM.csv",
        postcode_cache=DANISH_SOURCE + "postcode_coordinates.csv",
    output:
        powerplants=DANISH_CAPACITIES + "powerplants.csv",
        rooftop=DANISH_CAPACITIES + "existing_solar_rooftop_capacities.csv",
    log:
        "logs/danish_capacities/power.log",
    threads: 1
    resources:
        mem_mb=4000,
    script:
        "../scripts/prepare_danish_power_capacities.py"

rule prepare_danish_heat_capacities:
    input:
        ens=DANISH_SOURCE + "ENS_power_plant_register.csv",
    output:
        capacities=DANISH_CAPACITIES + "existing_chp_heat_capacities.csv",
    log:
        "logs/danish_capacities/heat.log",
    threads: 1
    resources:
        mem_mb=2000,
    script:
        "../scripts/prepare_danish_heat_capacities.py"

rule prepare_danish_rural_heat:
    input:
        households=DANISH_SOURCE + "JRC-IDEES-2023_Residential_DK.xlsx",
    output:
        capacities=DANISH_CAPACITIES + "existing_rural_decentral_heat_capacities.csv",
    log:
        "logs/danish_capacities/rural_heat.log",
    threads: 1
    resources:
        mem_mb=2000,
    script:
        "../scripts/prepare_danish_rural_heat.py"
