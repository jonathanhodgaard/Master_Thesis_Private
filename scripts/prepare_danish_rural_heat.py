"""Prepare Danish rural/decentral heat capacities from JRC household shares."""
import pandas as pd
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT_DIR / "data/danish_capacity_sources"
OUTPUT_DIR = ROOT_DIR / "data/existing_infrastructure"
PEAK_HEAT = 0.01 # MW per household; assumed 10 kW heating installation
URBAN_SHARE = 0.93 # From PyPSA-Eur
RURAL_SHARE = 1 - URBAN_SHARE
DH_SHARE = 0.7 # https://danskfjernvarme.dk/om-os/fakta-om-fjernvarme
DC_SHARE = URBAN_SHARE - DH_SHARE
TOT_HOUSEHOLDS = 2_019_107 / DH_SHARE # https://danskfjernvarme.dk/om-os/fakta-om-fjernvarme
DK1_DK2_RATIO = 0.55

MAPPING = {
    'Liquified petroleum gas (LPG)': 'gas boiler',
    'Diesel oil': 'oil boiler',
    'Natural gas': 'gas boiler',
    'Biomass': 'biomass boiler',
    'Advanced electric heating': 'air heat pump',
    'Conventional electric heating': 'resistive heater'

}

def main(source_dir=SOURCE_DIR, output_dir=OUTPUT_DIR):
    source_dir, output_dir = Path(source_dir), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load the residential heating data from the 'RES_hh_num' worksheet.
    df = pd.read_excel(
        source_dir / "JRC-IDEES-2023_Residential_DK.xlsx",
        sheet_name="RES_hh_num",
    )

    # Extract row 3 to 12 and the column 2023
    df_extracted = df.iloc[3:12,[df.columns.get_loc('DK - Number of households'), df.columns.get_loc(2023)]]

    # Remove the 'DK - Number of households' = 'Distributed heat'
    df_extracted = df_extracted[df_extracted['DK - Number of households'] != 'Distributed heat']

    # Calculate the share of each heating technology in the total number of households
    df_extracted['Share'] = df_extracted[2023] / df_extracted[2023].sum()

    # New column called installed rural heat capacity
    df_extracted['Installed Rural Heat'] = df_extracted['Share'] * RURAL_SHARE * TOT_HOUSEHOLDS * PEAK_HEAT

    # New column called installed decentral urban heat capacity
    df_extracted['Installed Decentral Urban Heat'] = df_extracted['Share'] * DC_SHARE * TOT_HOUSEHOLDS * PEAK_HEAT

    # Remove the original 2023 column and the row where 'DK - Number of households' is 'Distributed heat'

    df_extracted = df_extracted[df_extracted['DK - Number of households'] != 'Geothermal']
    df_extracted = df_extracted[df_extracted['DK - Number of households'] != 'Solids']

    # Map the 'DK - Number of households' column to the corresponding heating technology using the MAPPING dictionary.
    df_extracted['Heating Technology'] = df_extracted['DK - Number of households'].map(MAPPING)

    # Make new dataframe with the columns node, heat_system, set, heating_capacity
    # where heat system used both rural heat and decentral urban heat
    df_new = pd.DataFrame({
        'node': '',
        'heat_system': 'residential rural',
        'set': df_extracted['Heating Technology'],
        'heating_capacity': df_extracted['Installed Rural Heat']
    })
    df_new_decentral = pd.DataFrame({
        'node': '',
        'heat_system': 'residential urban decentral',
        'set': df_extracted['Heating Technology'],
        'heating_capacity': df_extracted['Installed Decentral Urban Heat']
    })

    df_new = pd.concat([df_new, df_new_decentral], ignore_index=True)

    # Split capacities between geographical DK1 (west) and DK2 (east).
    df_new['node'] = 'DK1'
    df_new_DK2 = df_new.copy()
    df_new_DK2['node'] = df_new_DK2['node'].str.replace('DK1', 'DK2')
    df_new = pd.concat([df_new, df_new_DK2], ignore_index=True)

    # use the DK1_DK2_RATIO to adjust the heating capacity for each row
    df_new.loc[df_new['node'] == 'DK1', 'heating_capacity'] *= DK1_DK2_RATIO
    df_new.loc[df_new['node'] == 'DK2', 'heating_capacity'] *= (1 - DK1_DK2_RATIO)

    # sum for same sets in same node and heat_system
    df_new = df_new.groupby(['node', 'heat_system', 'set'], as_index=False).agg({'heating_capacity': 'sum'})

    print(df_new)

    # Save as csv
    df_new.to_csv(output_dir / "existing_rural_decentral_heat_capacities.csv", index=False)

if __name__ == "__main__":
    if "snakemake" in globals():
        from contextlib import redirect_stdout
        with Path(snakemake.log[0]).open("w", encoding="utf-8") as log, redirect_stdout(log):
            main(Path(snakemake.input.households).parent, Path(snakemake.output.capacities).parent)
    else:
        import argparse
        parser = argparse.ArgumentParser(__doc__)
        parser.add_argument('--input-dir', type=Path, default=SOURCE_DIR)
        parser.add_argument('--output-dir', type=Path, default=OUTPUT_DIR)
        args = parser.parse_args()
        main(args.input_dir, args.output_dir)
