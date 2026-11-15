# Thesis changes relative to PyPSA-Eur v2026.08.0

## Baseline and scope

This thesis fork is based on [PyPSA-Eur v2026.08.0](https://github.com/PyPSA/pypsa-eur/releases/tag/v2026.08.0).
The comparison below is against that exact release, not v0.8.0 or the current upstream default branch.
It documents the working tree inspected on 15 November 2026; notebooks are excluded.

- Upstream release commit: `a5408e9db5402c53345d7339fffb52afe96d6e43`.
- Thesis fork commit at inspection: `72da95599167fc2fd93ef95d2fd3adb23629e6e7`.
- Companion repository: [fredersp/master_thesis](https://github.com/fredersp/master_thesis), containing Danish data preparation and an earlier fuel-price-shock workflow. Companion scripts are separate from this fork and are described below.

The main extensions are Danish existing-infrastructure calibration, geographical capacity controls,
country-specific transport electrification, household heating allocation, and fixed-capacity disruption dispatch.
The upstream installation, model description and licensing remain in the [main README](../README.md).

## Changes to existing scripts

### `scripts/prepare_sector_network.py`

- **Preserve conventional generation:** capture existing electrical generator capacity before the electricity-network patch removes those generators. Recreate it as sector Links with `p_nom` and `p_nom_min`, converting electrical output MW to fuel-input MW using efficiency.
- **Import existing Danish infrastructure:** apply CHP/district-heating, residential rural/decentral heating, and rooftop-PV CSVs after technology creation and heat-bus clustering. `sector.existing_infrastructure.apply_in_years` selects planning years; `keep_extendable` chooses a capacity floor or fixed equipment.
- **CHP and heat units:** CHP electrical output determines fuel-input nominal capacity; observed heat/electricity ratios update `efficiency2`. Boilers and resistive heaters convert heat-output MW to input MW. Reversed heat-pump Links use heat-output ratings directly. Solar thermal uses Generators. Missing target equipment is logged and skipped.
- **Additional district-heating technologies:** support coal/oil CHP fuel buses, use fuel-specific CHP cost assumptions when available (otherwise gas CHP assumptions), and add urban-central biomass, oil and waste boilers. Heat-only waste uses waste-CHP heat efficiency as an approximation.
- **Rooftop PV:** import capacity at low-voltage buses, sum duplicate node records, distribute across resource classes by potential, and raise estimated potential where necessary to accommodate installed capacity. Remove the same installations from utility PV inputs to avoid double counting.
- **Danish node aliases:** identify western/eastern Danish AC nodes by longitude and resolve bidding-zone aliases `DK1`/`DK2`; support explicit `sector.existing_capacity_node_map` for legacy CSV labels. The current implementation expects exactly two Danish AC nodes, so these thesis settings require the intended Danish clustering.
- **Freeze selected infrastructure:** `sector.fix_existing_capacity` selects countries/years, allows carrier exceptions, blocks specified new technologies or carrier suffixes, and fixes pit storage energy and charger/discharger ratings.
- **General capacity overrides:** `sector.capacity_overrides` selects equipment by component, exact carrier/name and geography, sets aggregate minimum capacity and/or expansion flags, and allocates only the shortfall. Units are MW, MWh or MVA according to component. Link output ratings require an explicit output basis and static positive efficiency. Overrides run after existing-capacity imports and fixing; overlapping selections, unmatched equipment and infeasible potentials fail validation. See [capacity-overrides.md](capacity-overrides.md).
- **Country/year EV shares:** use `sector.land_transport_electric_share_by_country`, falling back to the upstream electric share when absent. ICE becomes `1 - electric - fuel_cell`; invalid shares fail. These shares apply to the model's combined land-transport demand, so interpreting passenger-car shares as all-transport shares is a modelling assumption. ICE demand multiplication now occurs before column renaming to retain node alignment.
- **Preserve household demand through heat clustering:** keep residential load identities when allocation is enabled, distinguishing them from services/agriculture.
- **Optional biomass use:** `biomass.enforce_minimum_use: false` removes compulsory historical biogas/biomass/bioliquid use while retaining annual upper allowances. Waste/HVC treatment is handled separately.
- Convert heat-system enum entries to strings when selecting heat-load carriers.

### Other existing scripts

| File | Applied change |
| --- | --- |
| `scripts/solve_network.py` | Prepare household topology before optimisation, add household constraints, export heat shares and optional resource-budget duals, preserve custom network metadata, and allow `create_optimization_model` to receive a snapshot window. Reject annual budget-dual export from rolling investment solves. Handle missing/nullable `Link.reversed` flags safely. |
| `scripts/build_hydro_profile.py` | Convert EIA reference-year labels to strings and sort after adding missing years, matching Atlite's reference-year slicing. |
| `scripts/build_retro_cost.py` | Replace chained/in-place pandas assignments, update `stack` usage, and aggregate transposed heat-transfer data by column level. These are compatibility fixes, also referenced in local release notes. |
| `scripts/cluster_network.py` | Name the focus-weight index `cluster`, addressing clustering index compatibility. Also referenced in local release notes. |
| `scripts/lib/validation/config/sector.py` | Accept `sector.capacity_overrides`. |
| `scripts/lib/validation/config/biomass.py` | Accept `enforce_minimum_use` and `export_budget_duals`. |

`config/config.default.yaml` adds defaults for capacity overrides (empty), minimum biomass use (true),
and budget-dual export (false). `config/schema.default.json` adds the capacity-override schema entries;
the biomass options are defined in the Python validation model.

## New scripts and helper modules

| File | Purpose |
| --- | --- |
| `scripts/existing_capacity_nodes.py` | Resolve legacy node aliases, verify AC-bus targets, map capacity tables consistently and fix pit-storage energy/power ratings. |
| `scripts/household_heat_constraints.py` | Split rural/decentral household heating into technology groups, create separate optional tanks, constrain useful-heat shares, export shares, and freeze the solved allocation for dispatch. |
| `scripts/resource_budget_duals.py` | Export annual biomass/biogas/bioliquid supply-limit duals from Linopy to CSV and network metadata, retaining raw solver signs. |
| `scripts/prepare_network_disruptions.py` | Freeze solved capacities and household shares, optionally transfer donor-weather inputs, prepare rolling waste/HVC treatment, add fuel-delivery backup Links and demand-limited electricity/heat load shedding, then export a prepared network. |
| `scripts/solve_disruption_dispatch.py` | Reuse sector constraints for full-horizon or rolling dispatch, check optimality and fixed capacities, export reports, and write Gurobi infeasibility diagnostics when applicable. |
| `scripts/rolling_disruption_dispatch.py` | Optimise overlapping windows, retain only implemented dispatch, carry storage and resource ledgers forward, and report costs and emissions without counting overlap twice. |
| `scripts/disruption_resource_budgets.py` | Read reference fuel allowances/duals, apply scarcity prices and optional remaining-allowance constraints, update consumption ledgers, and optionally replace the CO2 budget with its reference price. |
| `scripts/disruption_storage_msv.py` | Validate hourly inputs, initialise/carry storage, and apply annual-average reference storage-balance marginal values (MSVs) to selected storage flows, including charging credits. |
| `scripts/disruption_waste_hvc.py` | Replace annual waste/HVC inventories with uniform arrivals and a bounded backlog; adjust disposal accounting and enforce an empty backlog at the accounting-year end when requested. |
| `scripts/plot_disruption_dispatch.py` | Compare saved full-foresight and rolling electricity/heat dispatch; export PNG/PDF plots, hourly MW CSVs and validation/provenance JSON. Does not run optimisation. |

### Household heating interpretation

Shares represent useful-heat demand, not household counts. Where installed heating capacity exists,
shares are fixed from thermal-capacity proportions; otherwise shares are optimised.
Each technology receives its own optional water tank, with no imposed minimum tank size.
On merged rural/decentral buses, residential demand is allocated by these shares; remaining
services/agricultural demand is assigned to the gas-boiler group. This is an explicit assumption.
The allocation formulation supports single-period overnight, full-horizon investment solves and requires
compatible tank templates and disabled residential building DSM. Dispatch subsequently freezes the allocation.

### Disruption interpretation and limits

- Donor networks transfer operating inputs (loads, renewable availability, inflows, temperature-dependent efficiencies and other availability series) by asset name. Base equipment/capacities and date labels are retained. Calendar hours and snapshot weights must match; remove leap days consistently.
- Fuel backup Links provide parallel **delivery capacity**, still consuming upstream fuel. They do not create an independent emergency fuel stock. Their configured premium is added to the original delivery cost.
- Electricity/heat shedding is limited by instantaneous demand, including allocated household heat. The current 2025 overlay prices both at EUR 10,000/MWh.
- Rolling dispatch requires consecutive, unit-weight hourly snapshots, a successfully solved matching reference and continuous dispatch. Unit commitment and finite aggregate green-import budgets are currently rejected.
- Selected storage MSVs come from the annual mean of reference `mu_energy_balance` duals. Accounting stores are excluded. Reference fuel-budget duals must also be present when scarcity pricing is enabled; regenerate the matching base if absent.
- Resource allowances are **price-only by default** in the supplied disruption overlays (`enforce_allowances: false`): usage/exceedance is reported but an annual cap is not enforced unless enabled globally or by carrier.
- MSV charges, resource scarcity charges, physical operating costs and CO2 premiums are reported separately. The rolling objective sums implemented periods; overlapping window objectives must not be summed as total operating cost.
- These cases currently change weather/dispatch conditions; arbitrary asset-outage schedules or fuel-price-shock schedules are not configured by the disruption rule itself. The companion fuel-shock workflow is a separate implementation.

## New and modified workflow rules

| File / rule | Change and products |
| --- | --- |
| `Snakefile` | Conditionally include `rules/prepare_disruption.smk` when `disruption` configuration is supplied. |
| `rules/build_sector.smk`: `prepare_sector_network` | Declare three existing-infrastructure CSV inputs, configurable through `sector.existing_infrastructure`, with defaults under `data/existing_infrastructure/`. |
| `rules/build_sector.smk`: `build_daily_heat_demand`, `build_hourly_heat_demand` | Correct log filename suffixes from `.loc` to `.log`. |
| `rules/solve_overnight.smk`: `solve_sector_network` | Add conditional `household_heat_shares_*.csv` and `resource_budget_duals_*.csv` outputs. |
| New `rules/prepare_disruption.smk`: `dispatch_disruptions` | Aggregate solved-network and report targets for every enabled case. |
| New `rules/prepare_disruption.smk`: `prepare_disruption_network` | Read the solved base, optional weather donor and matching household-share CSV; write `<output_directory>/<case>/network_prepared.nc`. |
| New `rules/prepare_disruption.smk`: `solve_disruption_dispatch` | Write `network_solved.nc`, `household_heat_shares.csv`, `fuel_backup_dispatch.csv`, `resource_usage.csv`, `storage_msv.csv`, `window_summary.csv` and `dispatch_summary.csv`. Also declare solver/Python logs. |

Full-horizon dispatch retains the reporting filenames, but rolling-only tables can contain headers only.
Rolling runs additionally create per-window solver logs; failed Gurobi solves may produce `.txt`/`.ilp` IIS diagnostics.

## Scenario files and running the fork

| Added configuration | Role |
| --- | --- |
| `config/config.sector_2025.yaml` | Ten-node NL/DE/DK/SE/NO/FI/GB overnight baseline, planning year 2025, weather July 2013 through June 2014, hourly sector resolution, imported Danish infrastructure and capacity controls. Includes German PV capacity overrides, Danish district-heat settings and a configured CO2 price. |
| `config/config.sector_2025_OG.yaml` | Alternative/original 2025 comparison configuration; uses January-December 2013 snapshots. It is a thesis scenario, not an untouched copy of upstream defaults. |
| `config/config.sector_2035_w_V2G.yaml`, `config/config.sector_2035_wo_V2G.yaml` | 2035 overnight comparisons with EV DSM enabled and V2G respectively enabled/disabled. Existing CSV overlays are restricted to 2025; separately configured 2035 capacity-fixing settings still apply. |
| `config/config.weather_1965_66.yaml` | Prepare an alternative July 1965-June 1966 weather donor, separate resource prefix, historical ERA5 cutout, leap-day removal and fixed reference nuclear availability. |
| `config/config.disruption_dispatch_2025.yaml` | Matching 2025 disruption overlay. Currently enables `base_weather`, uses a 120-hour window / 24-hour overlap and no simulation cutoff. The historical-weather case is commented out. |
| `config/config.disruption_dispatch_2035_w_V2G.yaml`, `config/config.disruption_dispatch_2035_wo_V2G.yaml` | Matching 2035 disruption overlays; currently use 48-hour windows / 24-hour overlap and an exclusive `2013-07-15` cutoff for initial testing. |
| `config/config.sophia.yaml` | SOPHIA solver-directory override. |
| `config/config.scenario.default.yaml` | Draft scenario/network template with a placeholder network path; not a ready-to-run disruption overlay. |

Run commands from this fork's root with a compatible environment installed from its existing
`pixi.toml`/lockfile or the SOPHIA environment. Gurobi requires a working licence when selected.
The planning year labels technology/capacity assumptions; snapshot years label weather and operating time.

Build the solved 2025 baseline (first use `--dry-run` to inspect required jobs):

```bash
snakemake results/sector_2025/networks/base_s_10___2025.nc --configfiles config/config.sector_2025.yaml --cores 8
```

Prepare the historical-weather donor separately, only if that case is needed:

```bash
snakemake resources/weather_1965_66/sector_2025/networks/base_s_10___2025.nc --configfiles config/config.sector_2025.yaml config/config.weather_1965_66.yaml --cores 8
```

Dispatch requires the already solved base and matching heat-share CSV; weather cases additionally
require the prepared donor. Load the sector configuration first and disruption overlay last.
Use the restricted rule list so missing or stale external inputs cannot schedule capacity expansion or donor preparation:

```bash
snakemake dispatch_disruptions --allowed-rules dispatch_disruptions prepare_disruption_network solve_disruption_dispatch --configfiles config/config.sector_2025.yaml config/config.disruption_dispatch_2025.yaml --cores 8
```

Do not load the historical-weather preparation overlay into this dispatch command; select the donor
through `disruption.cases.<case>.weather_network`. Update all target paths after changing run names,
clustering, scenario options or planning horizons. For SOPHIA, insert `config/config.sophia.yaml`
between sector and disruption configurations.

Plot saved dispatch:

```bash
python scripts/plot_disruption_dispatch.py --region DK0 --title "Eastern Denmark"
```

The plotting command's defaults select the 2025 base and base-weather rolling result, showing up to
14 days. Use `--base`, `--rolling`, `--start`, `--end`, `--sectors` and `--output-dir` for other cases.
Model cluster labels are not bidding zones: in the current ten-node networks DK0 is east and DK2 is west.

Added Slurm submission scripts are `run_default.sbatch`, `run_sector_2025.sbatch`,
`run_sector_2025_OG.sbatch`, `run_sector_2035_w_V2G.sbatch`, `run_sector_2035_wo_V2G.sbatch`,
`run_dispatch_2025_1965_66.sbatch` and `run_dispatch_2025_base_weather.sbatch`.
They contain SOPHIA-specific paths, environment and resource requests. Review these before reuse;
the historical-weather script requires the corresponding case to be enabled.
See [sophia_run_guide.md](sophia_run_guide.md), whose example run names may differ from current configuration.

## Input data and companion repository

The fork adds `data/existing_infrastructure/existing_chp_heat_capacities.csv`,
`existing_rural_decentral_heat_capacities.csv` and `existing_solar_rooftop_capacities.csv`, plus
`data/powerplants/archive/0.8.1/powerplants.csv`. The archive directory's `0.8.1` label is not the
PyPSA-Eur release baseline. Existing `agg_p_nom_minmax.csv` and the two EIA hydro annual CSVs
have line-ending-only differences when compared ignoring end-of-line whitespace.

The companion repository prepares the Danish CSVs outside the fork's Snakemake workflow:

| Companion script | Inputs and applied behavior | Outputs |
| --- | --- | --- |
| `scripts/existing_power_capacities.py` | Read ENS conventional/wind/solar registers and original PPM data; select operating end-2025 assets; replace Danish PPM rows with electricity-only ENS plants and wind/solar; use manual offshore coordinates, UTM conversion and cached postcode centroids. Coordinate-less onshore wind/utility PV use an assumed 80% western share. The current PV split threshold is **1,000 kW**, despite older 6 kW comments. | `data/powerplants.csv`, `data/existing_solar_rooftop_capacities.csv`, and an updated `data/postcode_coordinates.csv` cache. |
| `scripts/existing_chp_heat_capacities.py` | Select active end-2025 district-heating-connected ENS plants, map postcodes to legacy DK node labels, classify CHP/boilers/electric heat/solar thermal, map biogas to solid biomass and aggregate electrical/thermal MW. | `data/existing_chp_heat_capacitites.csv` (existing filename spelling). |
| `scripts/existing_rural_decentral_heat.py` | Use JRC-IDEES 2023 household technology shares, exclude distributed heat and unsupported categories, assume 10 kW peak heat per household, urban share 0.93 and district heat 0.70, and split capacity 55% west / 45% east. | `data/existing_rural_decentral_heat_capacities.csv`. |
| `scripts/add_fuel_shock.py` | Multiply selected Generator-carrier marginal costs during the first configured number of calendar days, creating time-dependent cost columns from static values when needed. | Per-scenario `network_shocked.nc`. |
| `scripts/solve_dispatch.py` | Freeze optimal capacities and solve dispatch with the configured solver. | Per-scenario `network_solved.nc`. |
| `scripts/save_results.py` | Export each Generator's dispatch and each bus's marginal price. | Per-scenario `time_indexed_results.csv`; prices use `price_<bus>` column names. |
| `scripts/plot_results.py` | Exploratory price-heatmap comparisons; currently needs an undefined `DataFrame` annotation and hard-coded paths/bus labels corrected before use. `days` currently limits rows, not elapsed calendar days. | Interactive figures; plotting rule is commented out. |
| `scripts/plot_base_network.py` | Empty placeholder in the inspected companion working tree. The fork's same-named upstream plotting script is unchanged. | None. |

`rules/fuel_shock.smk` adds `all`, `apply_fuel_shock`, `solve_dispatch` and `save_results`.
`configs/config.fuel_shock.yaml` defines `BASE` and `GAS_150_30D` (CCGT marginal cost x1.5 for 30 days).
The rule currently references `config/config.fuel_shock.yaml`, while the actual companion directory is
`configs/`; correct that path before invoking `snakemake --snakefile rules/fuel_shock.smk`.

Before transferring companion CSVs, reconcile filenames and geography: the CHP output's
`capacitites` spelling differs from the fork's `capacities` input. Companion preparation uses legacy
`DK1 0AC` (west) / `DK0 0AC` (east), which may differ from the fork's current cluster names.
Use an explicit `sector.existing_capacity_node_map` or convert labels to bidding-zone aliases.
Copying files alone does not resolve node labels or ensure the two repositories contain the same data revision.

The companion's `scripts/archive/` contains `power_plant_matching.py` (older combined preparation,
6 kW split), `plot_network.py`, `plot_network_1.py` and `network_energistyrelsen_comparison.py`
(capacity plotting/ENS comparisons). Some archived root calculations assume the previous directory
location and need adjustment before reuse. `configs/archive/` contains earlier electricity/sector configurations;
`configs/plotting.default.yaml` supplies plotting settings. These are separate from the active fork workflow.

## Other differences and existing checks

- `config/create_scenarios.py` is absent relative to upstream; scenario files are supplied directly.
- `doc/capacity-overrides.md` and `doc/sophia_run_guide.md` are added; local release notes mention the retrofit and clustering fixes.
- `.gitignore` makes room for existing-infrastructure/powerplant inputs and permits notebooks. Notebook contents are excluded from this comparison.
- `.github/workflows/security-scan.yaml` updates the artifact-download action from v23 to v24. The logo image differs and `cutouts/.gitkeep` is absent.
- Only executable permissions change for `rules/build_electricity.smk`, `rules/retrieve.smk`,
  `scripts/add_electricity.py`, `scripts/build_biomass_potentials.py`, `scripts/build_electricity_demand.py`,
  `scripts/build_line_rating.py`, `scripts/build_osm_boundaries.py`, `scripts/build_powerplants.py`,
  `scripts/build_shapes.py`, `scripts/cluster_gas_network.py` and `scripts/prepare_network.py`.
  Their source content matches the release; they are not model-behavior changes.

Added existing test modules cover capacity overrides, capacity-node mapping, household heat,
disruption preparation, annual resource-budget duals and rolling dispatch:
`test_capacity_overrides.py`, `test_existing_capacity_nodes.py`, `test_household_heat_constraints.py`,
`test_disruption_preparation.py`, `test_resource_budget_duals.py` and `test_rolling_disruption_dispatch.py`.
This documentation update was checked against source and Git differences; it does not establish that
these tests or full solver/weather workflows were executed successfully.
