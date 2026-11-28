"""Prepare end-2025 Danish district heat and CHP capacities from ENS."""
import numpy as np
import pandas as pd

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT_DIR / "data/danish_capacity_sources"
OUTPUT_DIR = ROOT_DIR / "data/existing_infrastructure"
END_2025 = pd.Timestamp("2025-12-31")


FUELTYPE_MAPPING = {
    "olie": "Oil",
    "naturgas": "Natural Gas",
    "fast biomasse": "Solid Biomass",
    "biogas": "Biogas",
    "affald": "Waste",
    "vandkraft": "Hydro",
    "kul": "Hard Coal",
}


CARRIER_MAPPING = {
    "Natural Gas": "gas",
    "Oil": "oil",
    "Solid Biomass": "solid biomass",
    "Biogas": "biogas",
    "Waste": "waste",
    "Hydro": "hydro",
    "Hard Coal": "coal",
}


def normalize_postcode(series):
    """Convert Danish postcodes to four-character strings."""
    return (
        pd.to_numeric(series, errors="coerce")
        .astype("Int64")
        .astype("string")
        .str.zfill(4)
    )


def postcode_to_zone(postcode):
    """Return the Danish electricity zone for a postcode."""
    if int(str(postcode).strip()) > 4999:
        return "DK1"
    return "DK2"


def classify_electric_heat(fuel, technology):
    """Return the carrier and set for electric heat technologies."""
    if fuel == "elektricitet" and technology == "elpatron":
        return "ac", "resistive heater"

    if "varmepumpe" in technology:
        return "air", "heat pump"

    return None


def classify_solar_thermal(fuel, technology):
    """Return the carrier and set for solar thermal plants."""
    if fuel == "solenergi" or technology == "solvarme":
        return "solar thermal", "solar thermal"

    return None


def main(source_dir=SOURCE_DIR, output_dir=OUTPUT_DIR):
    source_dir, output_dir = Path(source_dir), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    power_plants = pd.read_csv(
        source_dir / "ENS_power_plant_register.csv",
        sep=";",
        decimal=",",
    )

    power_plants["skrotdato"] = pd.to_datetime(
        power_plants["skrotdato"],
        errors="coerce",
    )
    power_plants["idriftdato"] = pd.to_datetime(
        power_plants["idriftdato"],
        errors="coerce",
    )
    power_plants["elkapacitet_MW"] = pd.to_numeric(
        power_plants["elkapacitet_MW"],
        errors="coerce",
    )
    power_plants["varmekapacitet_MW"] = pd.to_numeric(
        power_plants["varmekapacitet_MW"],
        errors="coerce",
    )

    active_power_plants = (
        (
            power_plants["idriftdato"].isna()
            | (power_plants["idriftdato"] <= END_2025)
        )
        & (
            power_plants["skrotdato"].isna()
            | (power_plants["skrotdato"] > END_2025)
        )
        & (power_plants["Hovedbrændselsgruppe"] != "Ej i drift i 2025")
    )

    df_chp_heat = power_plants.loc[active_power_plants].copy()
    df_chp_heat = df_chp_heat.loc[
        df_chp_heat["fv_net"].notna()
        & df_chp_heat["fv_net"].astype(str).str.strip().ne("")
    ].copy()

    df_chp_heat["vaerk_postnr"] = normalize_postcode(
        df_chp_heat["vaerk_postnr"]
    )
    df_chp_heat = df_chp_heat.loc[
        df_chp_heat["vaerk_postnr"].notna()
    ].copy()
    df_chp_heat["node"] = df_chp_heat["vaerk_postnr"].map(
        postcode_to_zone
    )

    df_chp_heat["fuel_lower"] = (
        df_chp_heat["Hovedbrændselsgruppe"]
        .str.strip()
        .str.lower()
    )
    df_chp_heat["tech_lower"] = (
        df_chp_heat["anlaegstype_navn"]
        .str.strip()
        .str.lower()
    )

    electric_heat = df_chp_heat.apply(
        lambda row: classify_electric_heat(
            row["fuel_lower"],
            row["tech_lower"],
        ),
        axis=1,
    )
    is_electric_heat = electric_heat.notna()

    solar_thermal = df_chp_heat.apply(
        lambda row: classify_solar_thermal(
            row["fuel_lower"],
            row["tech_lower"],
        ),
        axis=1,
    )
    is_solar_thermal = solar_thermal.notna()

    df_conventional = df_chp_heat.loc[
        ~is_electric_heat & ~is_solar_thermal
    ].copy()
    df_conventional = df_conventional.loc[
        df_conventional["varmekapacitet_MW"].fillna(0) > 0
    ].copy()
    df_conventional["carrier"] = (
        df_conventional["fuel_lower"]
        .map(FUELTYPE_MAPPING)
        .map(CARRIER_MAPPING)
        .replace("biogas", "solid biomass")
    )
    df_conventional["set"] = np.where(
        (df_conventional["elkapacitet_MW"].fillna(0) > 0)
        & (df_conventional["varmekapacitet_MW"].fillna(0) > 0),
        "CHP",
        "boiler",
    )

    df_electric = df_chp_heat.loc[is_electric_heat].copy()
    df_electric = df_electric.loc[
        df_electric["varmekapacitet_MW"].fillna(0) > 0
    ].copy()
    electric_classification = electric_heat.loc[is_electric_heat]
    df_electric["carrier"] = electric_classification.map(lambda item: item[0])
    df_electric["set"] = electric_classification.map(lambda item: item[1])
    df_electric["elkapacitet_MW"] = np.nan

    df_solar_thermal = df_chp_heat.loc[is_solar_thermal].copy()
    df_solar_thermal = df_solar_thermal.loc[
        df_solar_thermal["varmekapacitet_MW"].fillna(0) > 0
    ].copy()
    solar_thermal_classification = solar_thermal.loc[is_solar_thermal]
    df_solar_thermal["carrier"] = solar_thermal_classification.map(
        lambda item: item[0]
    )
    df_solar_thermal["set"] = solar_thermal_classification.map(
        lambda item: item[1]
    )
    df_solar_thermal["elkapacitet_MW"] = np.nan

    df_chp_heat = pd.concat(
        [df_conventional, df_electric, df_solar_thermal],
        ignore_index=True,
    )

    existing_chp_heat = (
        df_chp_heat
        .groupby(["node", "carrier", "set"], as_index=False)
        .agg(
            power_capacity=(
                "elkapacitet_MW",
                lambda series: series.sum(min_count=1),
            ),
            heating_capacity=("varmekapacitet_MW", "sum"),
        )
        .sort_values(["node", "carrier", "set"])
        .reset_index(drop=True)
    )

    output_path = output_dir / "existing_chp_heat_capacities.csv"
    existing_chp_heat.to_csv(output_path, index=False)
    print(f"\nSaved CHP/heat dataset to: {output_path}")


if __name__ == "__main__":
    if "snakemake" in globals():
        from contextlib import redirect_stdout
        with Path(snakemake.log[0]).open("w", encoding="utf-8") as log, redirect_stdout(log):
            main(Path(snakemake.input.ens).parent, Path(snakemake.output.capacities).parent)
    else:
        import argparse
        parser = argparse.ArgumentParser(__doc__)
        parser.add_argument('--input-dir', type=Path, default=SOURCE_DIR)
        parser.add_argument('--output-dir', type=Path, default=OUTPUT_DIR)
        args = parser.parse_args()
        main(args.input_dir, args.output_dir)
