"""Build Danish electricity capacity datasets from ENS and PPM registers.

For postcode lookups, create a private API key at https://danskadresseapi.dk
and set the ADDR_KEY environment variable before the first run.
Source data are under data/danish_capacity_sources; prepared data go to
data/existing_infrastructure. Normal runs are offline and never change the cache.
Use --refresh-postcodes explicitly to look up missing postcodes and save them.
The key is only needed for an explicit refresh with uncached postcodes.
"""

import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import shape

ROOT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT_DIR / 'data/danish_capacity_sources'
OUTPUT_DIR = ROOT_DIR / 'data/existing_infrastructure'
END_2025 = pd.Timestamp('2025-12-31')
PV_CAPACITY_THRESHOLD_KW = 1000 # https://www.irena.org/-/media/Files/IRENA/Agency/Publication/2024/Mar/IRENA_Energy_taxonomy_2024.pdf
DK1_SHARE_TARGET = 0.8 # TODO: Find accurate value
POSTCODE_API_URL = 'https://api.danskadresseapi.dk/dawa/postnumre'
POSTCODE_REQUEST_DELAY = 0.3  # Stay below the Hobby limit of 5 requests/second.

# ENS fuel and technology names mapped to PPM conventions.
# TODO: Double-check the mapping
FUELTYPE_MAPPING = {
    'olie': 'Oil',
    'naturgas': 'Natural Gas',
    'fast biomasse': 'Solid Biomass',
    'biogas': 'Biogas',
    'affald': 'Waste',
    'vandkraft': 'Hydro',
    'kul': 'Hard Coal',
}
TECHNOLOGY_MAPPING = {
    'Gasturbine': 'OCGT',
    'Forbrændingsmotor': 'Combustion Engine',
    'Dampturbine': 'Steam Turbine',
    'Kombianlæg': 'CCGT',
    'Bioforgasn. m. FM': 'Combustion Engine',
    'Vandkraft': 'Run-Of-River',
    'Nødstrømsanlæg': 'Combustion Engine',
    'Organic Rankine (ORC)': np.nan,
}

# Carrier names used by the existing CHP and heat-capacity dataset.
CARRIER_MAPPING = {
    'Natural Gas': 'gas',
    'Oil': 'oil',
    'Solid Biomass': 'solid biomass',
    'Biogas': 'biogas',
    'Waste': 'waste',
    'Hydro': 'hydro',
    'Hard Coal': 'coal',
}

# Offshore wind farm overrides: (latitude, longitude).
WINDFARM_COORDS = {
    'Middelgrundens Havvindmøllepark': (55.6923, 12.6708),
    'Horns Rev 2': (55.6024, 7.5902),
    'Rødsand 2': (54.5265, 11.617),
    'Anholt havvindmøllepark 1': (56.6015, 11.2291),
    'Tunø Knob Vindmøllepark': (55.9693, 10.3553),
    'MIDDELGRUNDEN': (55.6923, 12.6708),
    'Rønland Havvindmøllepark 2': (56.6704, 8.2162),
    'Rødsand': (54.5254, 11.7597),
    'Horns Rev 1': (55.4882, 7.8407),
    'Rønland Havvindmøllepark 1': (56.6704, 8.2162),
    'Horns Rev 3': (55.6876, 7.6677),
    'Kriegers Flak A': (55.0194, 12.8299),
    'Kriegers Flak B': (55.0382, 12.9926),
    'Vesterhav Nord Mølle 1 til 21': (56.6999, 8.0446),
    'Vesterhav Syd TA31 Ringkøbing': (56.0329, 8.0267),
    'Nissum Bredning Vindpark': (56.6771, 8.2518),
}

# Convert ENS coordinates from ETRS89 / UTM 32N to WGS84.
UTM_TO_WGS84 = Transformer.from_crs('EPSG:25832', 'EPSG:4326', always_xy=True)


def postcode_to_coords(postcode):
    """Fetch a land-clipped postcode centroid in WGS84 from DanskAdresseAPI."""
    if pd.isna(postcode):
        return {'longitude': np.nan, 'latitude': np.nan}

    postcode = str(postcode).strip().zfill(4)
    if postcode == '9999':
        return {'longitude': np.nan, 'latitude': np.nan}

    api_key = os.environ.get('ADDR_KEY', '').strip()
    if not api_key:
        raise RuntimeError(
            f'Postcode {postcode} is not cached. Set ADDR_KEY to your private '
            'DanskAdresseAPI key, or supply data/danish_capacity_sources/postcode_coordinates.csv '
            'with postcode, longitude and latitude columns.'
        )

    # Space out uncached lookups; HTTP 429 can mean either speed or quota.
    for attempt in range(5):
        time.sleep(POSTCODE_REQUEST_DELAY)
        try:
            response = requests.get(
                f'{POSTCODE_API_URL}/{postcode}',
                params={'format': 'geojson', 'landpostnumre': '', 'srid': 4326},
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=10,
            )
        except requests.RequestException:
            print(f'Could not retrieve postcode: {postcode}')
            return {'longitude': np.nan, 'latitude': np.nan}

        if response.status_code in (401, 403):
            raise RuntimeError('DanskAdresseAPI rejected ADDR_KEY or its permissions.')
        if response.status_code != 429:
            break

        try:
            error = response.json()
        except ValueError:
            error = {}
        if 'quota_exceeded' in str(error).lower():
            raise RuntimeError(
                'DanskAdresseAPI reports that the monthly quota is exhausted. '
                'Cached coordinates remain available.'
            )

        if attempt == 4:
            raise RuntimeError(
                'DanskAdresseAPI still returns HTTP 429 after five attempts. '
                'Check the API call log for the rate-limit or quota reason. '
                'Successful lookups are saved; rerunning will reuse them.'
            )
        try:
            delay = float(response.headers.get('Retry-After', 2 ** attempt))
        except (TypeError, ValueError):
            delay = 2 ** attempt
        if delay > 60:
            raise RuntimeError(
                f'DanskAdresseAPI asks for a {delay:g}-second wait. '
                'Rerun later; successful lookups are saved in the cache.'
            )
        delay = max(delay, POSTCODE_REQUEST_DELAY)
        print(f'Postcode {postcode}: rate limited; retrying in {delay:g} seconds.')
        time.sleep(delay)

    if response.status_code != 200:
        print(f'Could not find postcode: {postcode} (HTTP {response.status_code})')
        return {'longitude': np.nan, 'latitude': np.nan}

    try:
        data = response.json()
    except ValueError:
        print(f'Invalid response for postcode: {postcode}')
        return {'longitude': np.nan, 'latitude': np.nan}
    if not data.get('geometry'):
        print(f'No geometry for postcode: {postcode}')
        return {'longitude': np.nan, 'latitude': np.nan}

    # Keep the same centroid calculation as the original DAWA lookup.
    centroid = shape(data['geometry']).centroid
    return {'longitude': centroid.x, 'latitude': centroid.y}


def load_postcode_coords(postcodes, cache_path, refresh=False):
    """Read cached coordinates and fetch only missing postcodes."""
    cache_path = Path(cache_path)
    columns = ['postcode', 'longitude', 'latitude']
    cached = {}
    if cache_path.exists():
        table = pd.read_csv(cache_path, dtype={'postcode': 'string'})
        table['postcode'] = normalize_postcode(table['postcode'])
        for column in ['longitude', 'latitude']:
            table[column] = pd.to_numeric(table[column], errors='coerce')
        table = table.dropna(subset=columns)
        table = table.loc[
            np.isfinite(table['longitude']) & np.isfinite(table['latitude'])
        ]
        cached = table.set_index('postcode')[columns[1:]].to_dict('index')

    coordinates = {}
    for postcode in postcodes:
        # ENS uses 9999 for unknown locations; the original lookup never queried it.
        if pd.isna(postcode) or str(postcode) == "9999":
            coordinates[postcode] = {"longitude": np.nan, "latitude": np.nan}
            continue
        if postcode in cached:
            coordinates[postcode] = cached[postcode]
            continue

        if not refresh:
            raise ValueError(f"Postcode {postcode} is missing from {cache_path}; explicitly refresh the cache before preparing model inputs.")
        coords = postcode_to_coords(postcode)
        coordinates[postcode] = coords
        if not all(np.isfinite(value) for value in coords.values()):
            continue  # Failed lookups are retried on the next run.

        cached[postcode] = coords
        # Save each successful lookup so an interrupted run can resume.
        table = pd.DataFrame([
            {'postcode': code, **values} for code, values in cached.items()
        ], columns=columns).sort_values('postcode')
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_suffix('.tmp')
        table.to_csv(temporary_path, index=False)
        # Briefly retry transient Windows locks (for example from sync software).
        for attempt in range(3):
            try:
                temporary_path.replace(cache_path)
                break
            except PermissionError as exc:
                if attempt == 2:
                    raise PermissionError(
                        f'Cannot update {cache_path}. Close it in Excel or any '
                        'other program and check that the file is writable. '
                        f'The updated cache is retained at {temporary_path}.'
                    ) from exc
                time.sleep(0.5)

    return coordinates


def normalize_postcode(series):
    """Convert postcodes to four-character strings, preserving missing values."""
    return (
        pd.to_numeric(series, errors='coerce')
        .astype('Int64')
        .astype('string')
        .str.zfill(4)
    )


def postcode_to_node(postcode):
    """Return the Danish electricity node for a normalized postcode."""
    zone = 'DK1' if int(postcode) > 4999 else 'DK0'
    return 'DK1' if zone == 'DK1' else 'DK2'


def distribute_missing_coordinates(df, dk1_share=DK1_SHARE_TARGET, seed=0):
    """Give coordinate-less onshore wind/PV a DK1 or DK0 location.

    Installations with coordinates keep their node (from the postcode, or
    from the longitude if the postcode is missing). The remaining ones are
    assigned, largest first, to the zone furthest below its target share of
    the total capacity, and receive the coordinates of a randomly drawn
    located installation of the same technology in that zone.
    """
    df = df.copy()
    in_scope = df['Technology'].isin(['Onshore', 'PV'])
    located = df['lat'].notna() & df['lon'].notna()
    zone = pd.Series(pd.NA, index=df.index, dtype='object')
    has_postcode = df['Postnr.'].notna()
    zone[has_postcode] = df.loc[has_postcode, 'Postnr.'].map(
        lambda p: 'DK1' if int(p) > 4999 else 'DK0'
    )
    no_postcode = ~has_postcode & located
    zone[no_postcode] = np.where(df.loc[no_postcode, 'lon'] < 10.95, 'DK1', 'DK0')

    capacity = df['InstalleretkW'] / 1000
    base = in_scope & located
    totals = {z: capacity[base & (zone == z)].sum() for z in ('DK1', 'DK0')}
    pending = df.loc[in_scope & ~located].sort_values(
        'InstalleretkW', ascending=False
    )
    target_total = sum(totals.values()) + capacity[pending.index].sum()
    targets = {'DK1': dk1_share * target_total, 'DK0': (1 - dk1_share) * target_total}
    print('\n-------------------------------------')
    print('Onshore wind + PV > 1,000 kW by zone before distribution [MW]:')
    print({z: round(v, 2) for z, v in totals.items()})
    print(f'Capacity to distribute: {capacity[pending.index].sum():.2f} MW')

    if pending.empty:
        return df
    pools = {
        (z, t): df.loc[base & (zone == z) & (df['Technology'] == t), ['lat', 'lon']]
        for z in ('DK1', 'DK0')
        for t in ('Onshore', 'PV')
    }
    rng = np.random.default_rng(seed)
    for idx, row in pending.iterrows():
        gaps = {z: targets[z] - totals[z] for z in totals}
        chosen = max(gaps, key=gaps.get)
        pool = pools[(chosen, row['Technology'])]
        if pool.empty:
            pool = pd.concat([pools[(chosen, 'Onshore')], pools[(chosen, 'PV')]])
        if pool.empty:
            continue
        pick = pool.iloc[rng.integers(len(pool))]
        df.loc[idx, ['lat', 'lon']] = pick.to_numpy()
        df.loc[idx, 'coordinate_source'] = 'Distributed'
        zone[idx] = chosen
        totals[chosen] += capacity[idx]

    total = sum(totals.values())
    print('After distribution [MW]:')
    print({z: round(v, 2) for z, v in totals.items()})
    print(f'DK1 share: {totals["DK1"] / total:.2%}, DK0 share: {totals["DK0"] / total:.2%}')
    return df


def compare_wind_solar_outputs(
    wind_and_solar, active_wind_solar, powerplants_path, small_pv_path
):
    """Compare active ENS wind/PV capacity with the generated CSV outputs."""
    raw = wind_and_solar.loc[active_wind_solar].copy()
    powerplants = pd.read_csv(powerplants_path)

    small_pv = pd.read_csv(small_pv_path)
    powerplant_wind = powerplants.loc[
        (powerplants['Country'] == 'Denmark')
        & (powerplants['Fueltype'] == 'Wind')
    ]
    powerplant_large_pv = powerplants.loc[
        (powerplants['Country'] == 'Denmark')
        & (powerplants['Fueltype'] == 'Solar')
        & (powerplants['Technology'] == 'PV')
    ]
    comparisons = [
        (
            'Wind',
            raw['Kategori'].ne('Solcelle'),
            powerplant_wind,
            powerplant_wind['Capacity'].sum(),
        ),
        (
            f'PV > {PV_CAPACITY_THRESHOLD_KW} kW',
            (
                (raw['Kategori'].eq('Solcelle'))
                & (raw['InstalleretkW'].gt(PV_CAPACITY_THRESHOLD_KW))
            ),
            powerplant_large_pv,
            powerplant_large_pv['Capacity'].sum(),
        ),
        (
            f'PV <= {PV_CAPACITY_THRESHOLD_KW} kW',
            (
                (raw['Kategori'].eq('Solcelle'))
                & (raw['InstalleretkW'].le(PV_CAPACITY_THRESHOLD_KW))
            ),
            small_pv,
            small_pv['capacity'].sum(),
        ),
    ]
    print('\nWind and PV capacity assignment check')
    print(
        f"{'Category':<18} {'Raw installations':>18} "
        f"{'Raw MW':>12} {'CSV rows':>10} "
        f"{'Assigned MW':>14} {'Unassigned MW':>15}"
    )
    for category, raw_mask, output, assigned_capacity in comparisons:
        raw_category = raw.loc[raw_mask]
        raw_capacity = raw_category['InstalleretkW'].sum() / 1000
        unassigned_capacity = raw_capacity - assigned_capacity
        if abs(unassigned_capacity) < 0.0005:
            unassigned_capacity = 0.0
        print(
            f'{category:<18} {len(raw_category):>18,} '
            f'{raw_capacity:>12.3f} {len(output):>10,} '
            f'{assigned_capacity:>14.3f} {unassigned_capacity:>15.3f}'
        )
        missing_capacity_count = int(raw_category['InstalleretkW'].isna().sum())
        if missing_capacity_count:
            print(
                f'  {missing_capacity_count:,} raw installations have '
                'no capacity and are excluded from the MW comparison.'
            )


def main(source_dir=SOURCE_DIR, output_dir=OUTPUT_DIR, refresh_postcodes=False):
    source_dir, output_dir = Path(source_dir), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Load the ENS registers and the original PPM dataset.
    power_plants = pd.read_csv(
        source_dir / 'ENS_power_plant_register.csv', sep=';', decimal=','
    )
    wind_and_solar = pd.read_csv(
        source_dir / 'ENS_wind_&_solar_register.csv', sep=';', decimal=','
    )
    powerplantmatching = pd.read_csv(source_dir / 'powerplants_PPM.csv')

    # Keep PPM plants operating in 2025; missing dates remain eligible.
    active_ppm = (
        (powerplantmatching['DateOut'].isna() | (powerplantmatching['DateOut'] > 2025))
        & (powerplantmatching['DateIn'].isna() | (powerplantmatching['DateIn'] <= 2025))
    )
    df_ppm = powerplantmatching.loc[active_ppm].copy()
    total_ppm_capacity = df_ppm.loc[df_ppm['Country'] == 'Denmark', 'Capacity'].sum()
    print(f'Total capacity in Denmark in PPM dataset: {total_ppm_capacity:.2f} MW')

    # Parse ENS dates and capacities; invalid values become missing.
    for column in ['Afmeldt dato', 'Idriftsdato']:
        wind_and_solar[column] = pd.to_datetime(
            wind_and_solar[column], errors='coerce', format='mixed', dayfirst=True
        )
        
    for column in ['skrotdato', 'idriftdato']:
        power_plants[column] = pd.to_datetime(
            power_plants[column], errors='coerce', format='mixed', dayfirst=True
        )
        
    for column in ['elkapacitet_MW', 'varmekapacitet_MW']:
        power_plants[column] = pd.to_numeric(power_plants[column], errors='coerce')
        
    wind_and_solar['InstalleretkW'] = pd.to_numeric(
        wind_and_solar['InstalleretkW'], errors='coerce'
    )

    # Select installations operating at the end of 2025.
    active_wind_solar = (
        (wind_and_solar['Idriftsdato'].isna()
         | (wind_and_solar['Idriftsdato'] <= END_2025))
        & (wind_and_solar['Afmeldt dato'].isna()
           | (wind_and_solar['Afmeldt dato'] > END_2025))
    )
    
    df_w_pv = wind_and_solar.loc[active_wind_solar].copy()
    active_power_plants = (
        (power_plants['idriftdato'].isna()
         | (power_plants['idriftdato'] <= END_2025))
        & (power_plants['skrotdato'].isna()
           | (power_plants['skrotdato'] > END_2025))
        & (power_plants['Hovedbrændselsgruppe'] != 'Ej i drift i 2025')
    )
    
    df_pp = power_plants.loc[active_power_plants].copy()
    df_pp = df_pp.loc[df_pp['elkapacitet_MW'] > 0].copy()

    # Compare installed electricity capacity in MW before excluding CHP.
    total_wind_solar_capacity = df_w_pv['InstalleretkW'].sum() / 1000
    total_pp_capacity = df_pp['elkapacitet_MW'].sum()
    total_ens_capacity = total_wind_solar_capacity + total_pp_capacity
    
    print(
        'Total wind and solar capacity in Denmark in ENS dataset: '
        f'{total_wind_solar_capacity:.2f} MW'
    )
    
    print(
        'Total other power plant capacity in Denmark in ENS dataset: '
        f'{total_pp_capacity:.2f} MW'
    )
    
    print(f'Total capacity in Denmark in ENS dataset: {total_ens_capacity:.2f} MW')

    # Conventional plants: retain electricity-only plants and map their types.
    df_pp['vaerk_postnr'] = normalize_postcode(df_pp['vaerk_postnr'])
    df_pp = df_pp.loc[df_pp['varmekapacitet_MW'].fillna(0) <= 0].copy()
    df_pp['Set'] = 'PP'
    df_pp['Hovedbrændselsgruppe'] = (
        df_pp['Hovedbrændselsgruppe'].str.lower().map(FUELTYPE_MAPPING)
    )
    df_pp['anlaegstype_navn'] = df_pp['anlaegstype_navn'].map(TECHNOLOGY_MAPPING)

    # Preserve the original lowercase fuel comparison for identical mapping.
    # Mapped fuel is "Natural Gas", so it also differs from "natural gas".
    df_pp['anlaegstype_navn'] = np.where(
        (
            (df_pp['anlaegstype_navn'].isin(['CCGT', 'OCGT']))
            & (df_pp['Hovedbrændselsgruppe'] != 'natural gas')
        ),
        'Steam Turbine',
        df_pp['anlaegstype_navn'],
    )

    # Classify wind and PV installations using the ENS category and location.
    df_w_pv['Postnr.'] = normalize_postcode(df_w_pv['Postnr.'])
    df_w_pv['Kategori'] = np.where(df_w_pv['Kategori'] == 'Solcelle', 'Solar', 'Wind')
    df_w_pv['Technology'] = np.select(
        [
            (df_w_pv['Kategori'] == 'Wind') & (df_w_pv['Placering'] == 'HAV'),
            (df_w_pv['Kategori'] == 'Wind') & (df_w_pv['Placering'] == 'LAND'),
            df_w_pv['Kategori'] == 'Solar',
        ],
        ['Offshore', 'Onshore', 'PV'],
        default=pd.NA,
    )
    df_w_pv['Set'] = 'PP'

    # Aggregate PV up to 1,000 kW by electricity node; convert kW to MW.
    small_pv = df_w_pv.loc[
        (df_w_pv['Kategori'] == 'Solar')
        & (df_w_pv['InstalleretkW'] <= PV_CAPACITY_THRESHOLD_KW)
    ].copy()
    if small_pv['Postnr.'].isna().any():
        missing_postcodes = int(small_pv['Postnr.'].isna().sum())
        raise ValueError(
            f'Cannot assign {missing_postcodes} small PV installations '
            'to electricity nodes because their postcodes are missing.'
        )
    small_pv['node'] = small_pv['Postnr.'].map(postcode_to_node)
    small_pv_by_node = (
        small_pv.groupby('node', as_index=False)
        .agg(capacity=('InstalleretkW', 'sum'))
        .sort_values('node')
        .reset_index(drop=True)
    )
    small_pv_by_node['capacity'] /= 1000
    small_pv_output_path = output_dir / 'existing_solar_rooftop_capacities.csv'
    small_pv_by_node.to_csv(small_pv_output_path, index=False)
    print(f'\nSaved small PV capacity dataset to: {small_pv_output_path}')

    # Keep wind and PV above 1,000 kW in the power plant dataset.
    df_w_pv = df_w_pv.loc[
        (df_w_pv['Kategori'] != 'Solar')
        | (df_w_pv['InstalleretkW'] > PV_CAPACITY_THRESHOLD_KW)
    ].copy()

    # Conventional plant coordinates come from postcode centroids.
    postcodes_pp = df_pp['vaerk_postnr'].dropna().unique()
    postcode_coords_pp = load_postcode_coords(postcodes_pp, source_dir / "postcode_coordinates.csv", refresh_postcodes)
    df_pp['lon'] = df_pp['vaerk_postnr'].map(
        lambda p: postcode_coords_pp.get(p, {}).get('longitude', np.nan),
    )
    df_pp['lat'] = df_pp['vaerk_postnr'].map(
        lambda p: postcode_coords_pp.get(p, {}).get('latitude', np.nan),
    )

    # Wind/PV coordinates: manual override > UTM > postcode > missing.
    # ENS UTM values use periods for thousands and commas for decimals.
    for col in ['UTM x-koordinat', 'UTM y-koordinat']:
        df_w_pv[col] = (
            df_w_pv[col].astype('string')
            .str.replace('.', '', regex=False)
            .str.replace(',', '.', regex=False)
        )
        df_w_pv[col] = pd.to_numeric(df_w_pv[col], errors='coerce')
    mask_utm = df_w_pv['UTM x-koordinat'].notna() & df_w_pv['UTM y-koordinat'].notna()
    if mask_utm.any():
        (lon, lat) = UTM_TO_WGS84.transform(
            df_w_pv.loc[mask_utm, 'UTM x-koordinat'].to_numpy(dtype=float),
            df_w_pv.loc[mask_utm, 'UTM y-koordinat'].to_numpy(dtype=float),
        )
        df_w_pv.loc[mask_utm, 'lon'] = lon
        df_w_pv.loc[mask_utm, 'lat'] = lat

    # Look up postcode centroids only where UTM coordinates are unavailable.
    mask_postcode = (
        ~mask_utm
        & df_w_pv['Postnr.'].notna()
        & ~df_w_pv['Navn'].isin(WINDFARM_COORDS)
    )
    postcodes_w_pv = df_w_pv.loc[mask_postcode, 'Postnr.'].dropna().unique()
    postcode_coords_w_pv = load_postcode_coords(postcodes_w_pv, source_dir / "postcode_coordinates.csv", refresh_postcodes)
    df_w_pv.loc[mask_postcode, 'lon'] = df_w_pv.loc[mask_postcode, 'Postnr.'].map(
        lambda p: postcode_coords_w_pv.get(p, {}).get('longitude', np.nan),
    )
    df_w_pv.loc[mask_postcode, 'lat'] = df_w_pv.loc[mask_postcode, 'Postnr.'].map(
        lambda p: postcode_coords_w_pv.get(p, {}).get('latitude', np.nan),
    )

    # Apply manual wind farm coordinates last so they take priority.
    windfarm_overrides = df_w_pv['Navn'].map(WINDFARM_COORDS)
    mask_windfarm = windfarm_overrides.notna()
    df_w_pv.loc[mask_windfarm, 'lat'] = windfarm_overrides.loc[mask_windfarm].str[0]
    df_w_pv.loc[mask_windfarm, 'lon'] = windfarm_overrides.loc[mask_windfarm].str[1]

    # Report coordinate coverage and unassigned capacity.
    df_w_pv['coordinate_source'] = np.select(
        [mask_windfarm, mask_utm, mask_postcode],
        ['Manual wind farm', 'UTM', 'Postcode'],
        default='Missing',
    )
    print('\n-------------------------------------')
    print('Wind / solar coordinate sources')
    print('-------------------------------------')
    print(df_w_pv['coordinate_source'].value_counts())
    print('\nCapacity by coordinate source [MW]:')
    capacity_by_coordinate_source = (
        df_w_pv.groupby('coordinate_source')['InstalleretkW'].sum().div(1000)
    )
    print(capacity_by_coordinate_source)
    missing_coordinates = df_w_pv[df_w_pv['lat'].isna() | df_w_pv['lon'].isna()].copy()
    print('\nInstallations without coordinates:')
    print(len(missing_coordinates))
    print('Capacity without coordinates:')
    print(f"{missing_coordinates['InstalleretkW'].sum() / 1000:.2f} MW")
    if not missing_coordinates.empty:
        print('\nInstallations still missing coordinates:')
        print(
            missing_coordinates[
                ['Navn', 'Kategori', 'Technology', 'Postnr.',
                 'UTM x-koordinat', 'UTM y-koordinat', 'InstalleretkW']
            ]
        )

    # Distribute installations without coordinates so that onshore wind and
    # PV above 1,000 kW end up with an 80 % DK1 / 20 % DK0 capacity split.
    df_w_pv = distribute_missing_coordinates(df_w_pv)

    # Build PPM-compatible rows, preserving column order and missing fields.
    df_pp_final = pd.DataFrame(
        {
            'Name': (
                df_pp['fv_net_navn'].fillna('')
                + ' ('
                + df_pp['vrkanl_ny'].fillna('')
                + ')'
            ),
            'Fueltype': df_pp['Hovedbrændselsgruppe'],
            'Technology': df_pp['anlaegstype_navn'],
            'Set': df_pp['Set'].astype(str),
            'Country': 'Denmark',
            'Capacity': df_pp['elkapacitet_MW'],
            'Efficiency': np.nan,
            'DateIn': df_pp['idriftdato'].dt.year,
            'DateRetrofit': np.nan,
            'DateOut': df_pp['skrotdato'].dt.year,
            'lat': df_pp['lat'],
            'lon': df_pp['lon'],
            'Duration': np.nan,
            'Volume_Mm3': np.nan,
            'DamHeight_m': np.nan,
            'StorageCapacity_MWh': np.nan,
            'EIC': '{}',
            'projectID': '{}',
        }
    )
    df_w_pv_final = pd.DataFrame(
        {
            'Name': (
                df_w_pv['Navn'].fillna('').astype(str)
                + ' ('
                + df_w_pv['Stamdata GSRN'].fillna('').astype(str)
                + ')'
            ),
            'Fueltype': df_w_pv['Kategori'],
            'Technology': df_w_pv['Technology'],
            'Set': df_w_pv['Set'],
            'Country': 'Denmark',
            'Capacity': df_w_pv['InstalleretkW'] / 1000,
            'Efficiency': np.nan,
            'DateIn': df_w_pv['Idriftsdato'].dt.year,
            'DateRetrofit': np.nan,
            'DateOut': df_w_pv['Afmeldt dato'].dt.year,
            'lat': df_w_pv['lat'],
            'lon': df_w_pv['lon'],
            'Duration': np.nan,
            'Volume_Mm3': np.nan,
            'DamHeight_m': np.nan,
            'StorageCapacity_MWh': np.nan,
            'EIC': '{}',
            'projectID': '{}',
        }
    )

    # Replace Danish PPM rows with ENS data, then assign sequential IDs.
    df_ppm = df_ppm.loc[df_ppm['Country'] != 'Denmark'].copy()
    df_ppm = pd.concat([df_ppm, df_pp_final, df_w_pv_final], ignore_index=True)
    df_ppm['id'] = df_ppm.index

    # Save both datasets and compare wind/PV totals with the source register.
    output_path = output_dir / 'powerplants.csv'
    # Write to a temporary file first; writing directly to a file that is
    # locked or being synced can fail with OSError on Windows.
    temp_path = output_path.with_suffix('.csv.tmp')
    df_ppm.to_csv(temp_path, index=False)
    for attempt in range(5):
        try:
            os.replace(temp_path, output_path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(2)
    print(f'\nSaved processed dataset to: {output_path}')
    compare_wind_solar_outputs(
        wind_and_solar,
        active_wind_solar,
        output_path,
        small_pv_output_path,
    )

if __name__ == '__main__':
    if 'snakemake' in globals():
        from contextlib import redirect_stdout
        with Path(snakemake.log[0]).open("w", encoding="utf-8") as log, redirect_stdout(log):
            main(Path(snakemake.input.ens).parent, Path(snakemake.output.powerplants).parent)
    else:
        import argparse
        parser = argparse.ArgumentParser(__doc__)
        parser.add_argument('--input-dir', type=Path, default=SOURCE_DIR)
        parser.add_argument('--output-dir', type=Path, default=OUTPUT_DIR)
        parser.add_argument('--refresh-postcodes', action='store_true', help='Explicitly allow online lookups and update the postcode source cache')
        args = parser.parse_args()
        main(args.input_dir, args.output_dir, args.refresh_postcodes)
