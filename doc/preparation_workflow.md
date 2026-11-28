# Prepare inputs, build references, then run disruptions

Run everything from the `Master_Thesis_Private` repository root. This repository
now contains the Danish preparation code and source snapshots; no sibling checkout
or manual CSV transfer is required.

The dependency order is:

```text
Upstream source retrieval + pinned Danish source snapshots
    -> prepare Danish electricity/CHP/heating tables
    -> normal electricity and sector-network preparation
    -> full-year co-optimised infrastructure reference solve
    -> weather-donor operating profiles
    -> fixed-capacity rolling baselines
    -> separate electricity/heat timing analysis
    -> interconnector and fuel disruptions
    -> comparisons and plots
```

Snakemake retrieves each upstream input when its dependent rule needs it. It does
not need to finish every unrelated download first. The supplied Danish snapshots
are already inputs, so their preparation can run before weather/GIS downloads.
For thesis runs, the pinned original PPM snapshot feeds our preparation; the
standard vendor `retrieve_powerplants` branch remains available for other configs.

## 1. Environment and sources

Install the existing project environment, then create the top-level log directory:

```bash
pixi install
mkdir -p logs
```

On Sophia, use the established `.conda-env` and submission scripts instead of
creating a second environment. Solver access is needed for reference/dispatch
solves, but not for Danish CSV preparation.

Check that `data/danish_capacity_sources/` contains the five files listed in its
README and `manifest.json`. The raw JRC workbook is deliberately retained despite
the general spreadsheet ignore rule. These are versioned snapshots, not files to
retrieve from `master_thesis` at runtime. No API key or `.env` file was migrated.
Normal preparation is offline; missing real postcode coordinates cause a clear
error asking for an explicit cache refresh.

## 2. Prepare and inspect our model input tables

```bash
pixi run snakemake prepare_danish_capacities --cores 1 --configfiles config/config.sector_2025.yaml
```

On Sophia:

```bash
sbatch run_prepare_danish_capacities.sbatch
```

Wait for this shared preparation to finish before starting independent model
builds. It runs three rules and produces four shared model inputs:

| Rule / script | Output under `data/existing_infrastructure/` |
| --- | --- |
| `prepare_danish_power_capacities` / `scripts/prepare_danish_power_capacities.py` | `powerplants.csv`, `existing_solar_rooftop_capacities.csv` |
| `prepare_danish_heat_capacities` / `scripts/prepare_danish_heat_capacities.py` | `existing_chp_heat_capacities.csv` |
| `prepare_danish_rural_heat` / `scripts/prepare_danish_rural_heat.py` | `existing_rural_decentral_heat_capacities.csv` |

Logs are under `logs/danish_capacities/`. Each script also accepts standalone
`--input-dir` and `--output-dir` arguments. For example, run
`python -m scripts.prepare_danish_power_capacities --output-dir tmp/danish_capacity_review`
to review the electricity/rooftop tables without replacing active inputs.
Normal model builds depend on the prepared files automatically; the aggregate
target is a convenient way to prepare/inspect them before the larger workflow.
They are shared across 2025, both 2035 variants and weather years, not regenerated
for every disruption.

### Preserved preparation assumptions

- Select assets operating at end-2025; original PPM rows with missing commissioning
  or retirement dates remain eligible. Other countries retain the active PPM
  snapshot entries. Danish PPM entries are replaced using ENS.
- Keep electricity-only conventional installations in the powerplant table;
  district-heating/CHP capacities use their separate table.
- Split PV at **1,000 kW**. Smaller/equal PV is supplied through the rooftop table;
  larger PV remains in the powerplant table. Do not add rooftop PV to the utility
  fleet a second time.
- Preserve manual offshore locations, UTM conversion, postcode centroids and the
  seeded coordinate-allocation fallback with an 80% western target. Invalid
  `9999` postcodes remain unknown rather than triggering online requests.
- District-heating classification, biogas-to-solid-biomass mapping and the original
  conventional fuel/technology mapping are unchanged by migration.
- Rural/decentral heat retains JRC 2023 shares, 10 kW per household, 93% urban share,
  70% district heating, `2,019,107 / 0.70` total households and a 55% west/45% east split.
- Export geographical **DK1/DK2 aliases**, resolved by the model's existing mapping.
  In the ten-node model, geographical DK2 is cluster `DK0 0AC`; cluster names are
  not bidding-zone labels. The CHP filename typo is corrected.

The three smaller tables reproduced the old private data numerically. The
powerplant CSV exactly reproduced the current companion output, which differs
from the old private archive. See `danish_capacity_migration.json` for the
comparison: total rows 201,204 -> 163,099; Danish solar within the powerplant table
4,467.47685 -> 3,268.80507 MW. The separate rooftop total remains 1,799.51861 MW.
This migration preserves the companion recipe, not the older private archive's
capacity contents. No additional scientific remapping was introduced.

### Prepared versus downloaded powerplants

All three active sector configs explicitly set:

```yaml
electricity:
  prepared_powerplants: data/existing_infrastructure/powerplants.csv
```

`build_powerplants` reads this file before normal country filtering, bus mapping
and downstream conventional/renewable capacity assignment. Both conventional
plants and wind/PV capacity estimation therefore use the same prepared fleet.
The sector rules consume the other three files via their existing paths.

`electricity.prepared_powerplants: null` retains normal PyPSA-Eur retrieval.
The downloaded archive path is never a preparation output. The former calibrated
archive file was moved intact to
`data/existing_infrastructure/archive/powerplants_pre_migration.csv`; use it only
when explicitly reproducing a historical input. It is not labelled as original
PPM source data.

## 3. Prepare the electricity/sector network

For a preparation-only run, stopping before optimisation:

```bash
pixi run snakemake resources/sector_2025/networks/base_s_10___2025.nc --cores 4 --configfiles config/config.sector_2025.yaml
```

The normal PyPSA-Eur graph retrieves and prepares weather, geographical, demand,
cost and other required upstream data, runs any missing Danish preparation rules,
and constructs the sector-coupled network. Append `--dry-run` to inspect the graph.
Do not copy or edit a downloaded powerplant archive to inject custom capacities.

## 4. Solve co-optimised infrastructure references

```bash
pixi run snakemake results/sector_2025/networks/base_s_10___2025.nc --cores 4 --configfiles config/config.sector_2025.yaml
```

For 2035, use the matching `config.sector_2035_wo_V2G.yaml` or
`config.sector_2035_w_V2G.yaml` and corresponding `results/sector_2035_<variant>/`
path ending in `base_s_10___2035.nc`.

On Sophia, run the existing scripts in sequence, waiting for each shared model
build to finish:

1. Submit `sbatch run_sector_2025.sbatch` and wait for successful completion.
2. Submit `sbatch run_sector_2035_wo_V2G.sbatch` and wait for successful completion.
3. Submit `sbatch run_sector_2035_w_V2G.sbatch` and wait for successful completion.

These files do not add Slurm job dependencies automatically. Do not submit all
three at once against a shared working directory.
These are single-horizon, full-year perfect-foresight capacity-and-dispatch solves.
The PyPSA-Eur setting remains `foresight: overnight`; do not change it to `perfect`
(which denotes a different, multi-investment-period workflow) merely because
operation has perfect foresight within the year.

Running these targets may rebuild existing reference paths when inputs change.
The migration itself has not executed them or altered saved networks/results.
For a new study retaining old references, set a distinct `run.name`, use its new
resource/result target paths, and update the disruption variant reference and
household-share paths to that run. A rebuild with these migrated inputs must not
be presented as the same infrastructure calibration as an older saved reference.

## 5. Prepare weather donors

After the source references exist, prepare donor operating profiles with the
existing `run_weather_donors.sbatch` (2025) and `run_weather_donors_2035.sbatch`
(both 2035 variants). These scripts require the named historical cutouts first.
The four required archived cutouts live under `data/cutout/archive/v1.0/`:
`europe-1961-1966-era5.nc`, `europe-1966-1971-era5.nc`,
`europe-1986-1991-era5.nc`, and `europe-2016-2021-era5.nc`.
For a first-time build, the normal Snakemake donor target can retrieve its required
upstream archive inputs; the Sophia batch files precheck that the cutouts exist:

```bash
pixi run snakemake resources/weather_1965_66/sector_2025/networks/base_s_10___2025.nc --cores 4 --configfiles config/config.sector_2025.yaml config/config.weather_1965_66.yaml
```

Repeat with the other configured years/variants as needed. Donors are prepared resource
networks; no donor-specific capacity optimisation is needed for the fixed-capacity
campaign. Keep preparation jobs sequential where upstream files are shared.

## 6. Rolling baselines, timing and disruptions

Follow [Disruption campaign](disruption_campaign.md) after the chosen references
and donors have been built. In order:

1. `run_disruption_baselines.sbatch`: 15 fixed-capacity **rolling** no-disruption
   baselines and their event-window tables.
2. Review both sets of DK2 1/24/168/720/2160-hour windows: total electrical
   demand minus available wind/solar for interconnectors, and useful heat demand
   minus available wind/solar for fuel disruptions. The electrical criterion
   includes heat-pump/boiler electricity; the two scores are never added.
3. Submit `run_interconnector_<variant>.sbatch` and `run_fuel_<variant>.sbatch`.
4. Generate comparisons and selected detailed plots.

Use matching infrastructure references, household shares and donors. The campaign
records/rejects changed shared policies; existing historical results are not
silently reinterpreted under a new preparation policy. No disruption solve is a
replacement for the perfect-foresight infrastructure-reference solve.
