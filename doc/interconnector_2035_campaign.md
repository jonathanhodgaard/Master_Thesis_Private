# 2035 interconnector dispatch tests on Sophia

Run from `/work/users/s203684/projects/Master_Thesis_Private` after copying the
new files there. The two batch files select **one weather year per job** and
run its eight cases sequentially, starting with `base_weather` (no outage).
A failure stops that job before the next case. The original weather is the
default; historical years must be explicitly selected.

## Settings and interconnector comparison

Load, in order: the matching 2035 sector config, Sophia config,
`config.interconnector_disruption.yaml`, then the matching
`config.interconnector_disruption_2035_{w_V2G,wo_V2G}.yaml`.
The common config is the current 2025 campaign: all eight cases and outage dates,
168-hour horizon, 48-hour overlap, full July-June simulation, resource allowances,
opening inventories, hourly/carrier-specific storage prices, CO2 pricing,
weekly waste treatment, heat vents, fuel backups, shedding costs and solver tolerances.
The 2035 overlays change only the reference paths, planning horizon, output root
and donor paths. Do not add the older `config.disruption_dispatch_2035_*.yaml`
overlays: those contain different two-week/48-hour trial assumptions.

The three local solved references have identical names, endpoints and optimized
capacities for all **9 AC Lines and 38 directional DC Links** (including zero
capacity projects). In particular:

| Connection | Asset | Nominal capacity in all three references |
| --- | --- | --- |
| Eastern DK–Sweden | Line `4`: DK0 0AC–SE0 0AC | 3574.952867 MVA |
| Kontek | `relation/2505320-400-DC`, plus `-reversed` | 600 MW each direction |
| Great Belt | `relation/5487095-400-DC`, plus `-reversed` | 600 MW each direction |

The AC figure is the model's aggregated nameplate rating; operating limits use
its `s_max_pu` and loss constraints. Opposite DC directions represent the same
connection and must not be summed as physical capacity. DK0 is Eastern Denmark
(DK2 bidding zone); the model's DK2 cluster is Western Denmark.

Default Finland/Sweden distribution multipliers remain **1.15 / 1.02**, matching
2025. To test whether they are needed in 2035, use `NATIVE_LOW_VOLTAGE=1` below;
this sets both multipliers to 1.0 and selects a separate output root.
Do not mix the two output trees when comparing runs.

## 1. First test: original weather, no donor build required

```bash
mkdir -p logs
bash -n run_dispatch_2035_w_V2G.sbatch
bash -n run_dispatch_2035_wo_V2G.sbatch
sbatch run_dispatch_2035_w_V2G.sbatch
sbatch run_dispatch_2035_wo_V2G.sbatch
```

These are full-year **2013-2014 weather** tests, one job per model, eight cases
per job. Each requests an exclusive 32-CPU workq node and 48 hours; each Gurobi
solve retains eight threads. The time request covers eight sequential annual
cases; adjust to measured runtime. No job is submitted by the preparation work
in this checkout.

For a smaller initial feasibility test, submit just the no-outage full year:

```bash
sbatch run_dispatch_2035_w_V2G.sbatch --cases base_weather
sbatch run_dispatch_2035_wo_V2G.sbatch --cases base_weather
```

Then submit the full eight-case commands after those jobs finish; completed
case outputs can be reused. Do not submit overlapping jobs for the same output
folders. A dry run adds `--dry-run` to either batch command.

Optional native-distribution diagnostic (separate results):

```bash
sbatch --export=ALL,NATIVE_LOW_VOLTAGE=1 run_dispatch_2035_w_V2G.sbatch --cases base_weather
sbatch --export=ALL,NATIVE_LOW_VOLTAGE=1 run_dispatch_2035_wo_V2G.sbatch --cases base_weather
```

## 2. Prepare 2035 weather donors

A 2025 donor must not be substituted for a 2035 donor: 2035 has different EV,
heating and conversion assets/profiles. Existing weather overlays are reusable,
but apply each after the matching **2035 sector config** when building donors.

`run_weather_donors_2035.sbatch` builds all four years for both model variants,
sequentially, stopping at the prepared resource network (no investment solve).
Do not run it concurrently with another build writing shared preprocessing.
The archived cutouts must be readable under `data/cutout/archive/v1.0/`.
To build only the next weather year for both variants:

```bash
sbatch --export=ALL,WEATHER_YEARS=1964_65 run_weather_donors_2035.sbatch
```

Other suffixes are `1965_66`, `1968_69`, `1982_83`. Omitting `WEATHER_YEARS`
builds all four in that order. Preview a build before submitting, with the
Sophia environment active:

```bash
snakemake --dry-run --cores 1 \
  resources/weather_1964_65/sector_2035_w_V2G/networks/base_s_10___2035.nc \
  --configfiles config/config.sector_2035_w_V2G.yaml config/config.sophia.yaml \
                config/config.weather_1964_65.yaml
```

## 3. Then test one historical weather year at a time

After its donor job succeeds and the original-weather tests have been reviewed:

```bash
sbatch run_dispatch_2035_w_V2G.sbatch --years 1964-1965
sbatch run_dispatch_2035_wo_V2G.sbatch --years 1964-1965
```

Repeat these two commands, replacing the year with `1965-1966`, `1968-1969`,
then `1982-1983`, reviewing each year before proceeding. Each invocation runs
only that year, with the same eight cases. All selected inputs are checked
before the first case; missing donors cannot trigger weather/investment builds.
To first test a historical year's no-outage control, append `--cases base_weather`.

For task/input inspection without submission:

```bash
python -m scripts.run_disruption_campaign \
  --configfiles config/config.sector_2035_w_V2G.yaml config/config.sophia.yaml \
                config/config.interconnector_disruption.yaml \
                config/config.interconnector_disruption_2035_w_V2G.yaml \
  --years 1964-1965 --check-inputs
```

Replace `--check-inputs` with `--list` to display case ordering, or with
`--run-year --dry-run` to preview all cases. Use the `wo_V2G` paths for the other model.

Outputs are `results/disruptions/sector_2035_<variant>_interconnector_disruption/<year>/<case>/`.
Native-distribution tests use `sector_2035_<variant>_interconnector_native_low_voltage`.
The donor weather maps onto the retained July 2013–June 2014 reference calendar,
so outage timestamps remain identical to the 2025 campaign.

## Local verification on 19 November 2026

Both 2035 references passed structural preparation, all eight outage selectors
validated, and their first 168-hour production models constructed successfully.
The V2G model has 345681 variables / 757129 constraints; without V2G has
344001 / 753769. Both original-weather no-outage workflow dry runs passed;
13 campaign tests and all three new batch-file syntax checks passed.

Opening-week HiGHS tests **without** the FI/SE increases both reached their
180-second solver limit. These are inconclusive: they neither certify full-year
feasibility nor justify removing the increases. The local Gurobi test could not
start because its licence username differs from the execution account. Sophia
Gurobi solves, donor builds and historical-weather tests have not been run.
Machine-readable results: [dispatch_2035_preflight.json](dispatch_2035_preflight.json).

## Feasibility checks and failure diagnosis

The first Sophia V2G baseline solved window 0 optimally, then stopped while
building window 1 with `Constant values in objective function not supported`.
This was a Linopy objective-construction error, not an infeasible model. The
CO2 price now adds only the variable end-stock term to the solver objective;
the fixed opening-stock offset is applied in the reported window objective.
Net-emission premiums and dispatch incentives are preserved. Copy both
`scripts/disruption_resource_budgets.py` and `scripts/rolling_disruption_dispatch.py`
to Sophia before resubmitting affected jobs. The regression test covers three
overlapping windows with zero and nonzero opening atmospheric stocks; a single
opening-window test cannot exercise this failure path.

`python -m scripts.check_2035_dispatch --variant w_V2G --build-window --output tmp/check_w_V2G.json`
validates reference calendars/weights, interconnector parity, every outage,
fixed household shares, frozen capacities, waste handling, fuel-budget duals,
CO2-price replacement, storage duals and opening inventories, then constructs
the first production-sized 168-hour optimization model. Repeat with `wo_V2G`.
`--year 1964-1965` validates the matching donor once built; `--native-low-voltage`
checks native distribution capacities. `--solve-window` additionally attempts
that opening week with HiGHS and a 180-second solver limit, without changing
production Gurobi settings. Structural checks alone do not prove feasibility;
a timeout is inconclusive. A feasible opening week does not validate winter.

Relevant differences from 2025:

- Both 2035 references contain a binding annual CO2 limit. Rolling dispatch
  replaces it with the matching reference dual price, rather than incorrectly
  repeating an annual budget per window. The references price it at approximately
  665.23 EUR/t (V2G) and 667.67 EUR/t (without V2G).
- Both have EV battery Stores and BEV charging/availability/required energy
  profiles; only the V2G reference has the ten reverse V2G Links. Battery energy
  minima and low-voltage charging bottlenecks can constrain dispatch even when
  ordinary electricity loads permit shedding.
- Additional conversion routes include ammonia, hydrogen fuel cells, stationary
  batteries, biomass-to-methanol and capture equipment. Fixed capacities,
  commodity balances, heat disposal, material deadlines and inventory continuity
  remain enforced. Do not relax them pre-emptively.
- Resource allowance accounting, zero opening biomass/coal/oil inventories and
  storage value inputs are retained from the current 2025 experiment. Tiny
  optimized capacities and tighter fossil/biomass delivery can be numerically
  relevant. Gas retains the existing reference initialization assumption.

For actual infeasibility, `solve_disruption_dispatch.py` already disables Gurobi
DualReductions and saves `infeasibility_window_NNNN.txt`, `.ilp`, full-precision
`.mps`, and `.model.nc`, with a 300-second IIS search limit. Logs identify the
failed rolling window. Inspect the conflicting equations and full-precision
captures before changing assumptions; an IIS identifies a conflict set, not a
unique cause. `solver_window_NNNN.log` and the case `dispatch.log` are retained.
Successful cases also have `window_summary.csv`, `resource_usage.csv`,
`dispatch_summary.csv` and fuel-backup reports. Shedding is a feasible shortage
outcome and should be assessed separately from mathematical infeasibility.

Per-case plots are generated by each task. Shared comparison plots are separate,
as in the 2025 campaign guide; request `plot_disruptions` once after a year's
cases finish, using this same four-config stack and `disruption_weather_year`.
