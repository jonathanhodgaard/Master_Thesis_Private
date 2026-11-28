# Disruption campaign

First complete [source preparation and infrastructure references](preparation_workflow.md).
Danish capacity preparation, full-year co-optimised reference solves and weather
donor preparation precede this fixed-capacity rolling campaign.

The active campaign has three network variants (2025, 2035 without V2G, 2035 with
V2G), five weather years, and durations of 1, 24, 168, 720 and 2160 hours. It uses
15 no-disruption baselines, 525 interconnector disruptions, and 675 fuel-use
disruptions. All new outputs are under `results/disruption_campaign_v2/`.
Existing results and source networks are never rewritten by campaign jobs.

## Configuration

Load the matching `config.sector_<variant>.yaml`, optionally `config.sophia.yaml`,
then `config.disruption_common.yaml`. Append exactly one campaign config for
interconnectors or fuels. The shared runner selects this stack automatically.

- `config.disruption_common.yaml`: variants and input paths, weather donors,
  durations, storage/resource policy, shedding prices and preparation.
- `config.interconnector_disruption.yaml`: the three electrical connections;
  all seven nonempty combinations are generated.
- `config.fuel_disruption.yaml`: oil/gas/biomass consuming-Link availability
  reductions of 0.33, 0.66 and 1.0 in geographical DK2 (`DK0 0AC`).

Infrastructure is frozen from the matching solved investment network, across
weather years. Weather donors transfer operating inputs only and retain the
reference model calendar (July 2013Ã¢â‚¬â€œJune 2014). All external `solid biomass import`
Links are fixed at 50,000 MW each. The annual import allowance is independent
and unchanged. Heat vents are sized from connected heat inputs. All remaining
infrastructure capacities, including Finnish/Swedish distribution links, stay
at the base values. Recorded legacy delivery/distribution increases are restored.

Electricity shedding is 23,300 EUR/MWh and heat 100,000 EUR/MWh, with an additional
100 EUR/MWh in DK2. Oil/gas/biomass final-demand shedding costs 23,300 EUR/MWh;
hydrogen final-demand and process shortfall costs 200,000 EUR/MWh. The regional
adder currently applies to electricity and heat, matching the prior policy.
Fuel/H2 shedding is demand/consumption bounded, not unlimited emergency supply.

## Baseline and reference policy

Each variant/weather pair prepares one shared undisrupted network, then solves
one full-year rolling baseline (168-hour look-ahead, 48-hour overlap, 120 hours
committed per solve). Disruptions start from the same prepared input, not from
the baseline's future solved trajectory. Their full-year states evolve normally.

The original solved infrastructure reference supplies storage opening states,
storage marginal values, resource scarcity prices and allowances for every
weather donor. This retains the existing reference-based policy explicitly;
weather-specific perfect-foresight references are not newly optimised. Storage
opening overrides (empty physical biomass and shared coal/oil stocks) and
resource allowances are identical between controls and disruptions. The exact
reference path is recorded in each `run_info.json`.

## Event timing

Each solved rolling baseline produces two independently selected sets of DK2
windows. Both use available wind/solar electricity before curtailment:

| Campaign | Hourly timing score |
| --- | --- |
| Interconnector outages | Total electricity consumption - available wind/solar |
| Fuel disruptions | Useful heat demand - available wind/solar |

Electrical consumption includes fixed electricity Loads, heating electricity
(heat pumps, electric boilers and heating auxiliaries), other conversion inputs
(EV charging, electrolysis, etc.) and electrical charging. Ordinary/reverse-flow
heat pumps are included. Electricity transfers such as internal distribution and
interconnectors are not counted again. Heating electricity is **not subtracted**
from the interconnector criterion: it contributes to electrical import dependence.
The two criteria are evaluated separately, never added together.

Useful heat demand is the sum of final Loads at DK2 heat buses: district,
rural/decentral, industry and agriculture where connected to those buses. Count
the original household heat Load once, without adding its technology-specific
delivery Links. Thermal storage flows, production losses, vents and shedding are
not additional final heat demand. Requested heat is counted even if some is shed.

The fuel criterion is explicitly a **high-heat-demand/low-wind-and-solar proxy**,
not a physical heat deficit or a measure of fuel consumption. Wind/solar are
still measured as available electricity; no conversion using COP/boiler efficiency
is applied. All oil/gas/biomass reductions use the fuel campaign's heating windows.
Biomass, solar thermal and other heat sources are not subtracted from either score.

Renewable availability uses frozen `p_nom * p_max_pu`, including rooftop solar.
For each campaign and duration, scan every hourly starting point and maximise
the signed cumulative score. Keep negative surplus hours; consider only complete
windows within the year, without wrapping. The first maximum wins ties. Durations
are 1, 24, 168, 720 and 2160 hours, selected independently for each criterion.
The same-duration electrical and heating windows can have different dates.
Baseline demand can respond differently during disruptions; dates stay fixed
from the undisrupted baseline.

`residual_load_hourly.csv` records `electrical_residual_load_MW` and
`heat_stress_MW`, plus gross electricity, electricity used for heat, electricity
excluding heat, useful heat demand and available wind/solar. The heating-related
electricity breakdown is diagnostic only; the interconnector score uses the full
electrical total.

`event_windows.csv` contains **10 rows per variant/weather pair**: five per
campaign, identified by `campaign` and a distinct `timing_metric`. Across the 15
baselines this gives 150 candidate windows. It records selected component energies
and `residual_energy_MWh` (the cumulative score for that row's criterion). Dates
use the model calendar, with inclusive start/exclusive end and weather-year identity.
Application, task plans and campaign summaries select by campaign and duration;
obsolete combined/untagged tables cannot silently supply event dates. Summaries
also reject solved scenarios whose recorded criterion/dates differ from the
selected windows.

Regenerate the timing tables from existing solved rolling baselines before
submitting either campaign. This timing change alone requires no capacity change
or baseline optimisation. For example:

```bash
snakemake analyse_disruption_windows --cores 1 --allowed-rules analyse_disruption_windows --configfiles config/config.sector_2025.yaml config/config.disruption_common.yaml --config disruption_variant=2025 disruption_weather_year=2013-2014
```

Repeat for each existing variant/weather baseline. This command produces both
sets of windows and cannot schedule a baseline solve. Saved results are not
modified by the code change itself. Disrupted scenarios using previous timing
must be rerun or retained separately as historical experiments before making
comparisons under the new criteria.

Fuel disruptions scale the normal `Link.p_max_pu` by `1 - reduction` only within
that interval. Input capacities and efficiencies remain unchanged. Selection is
one boundary from raw fuel to conversion/end-use, attributed using the consuming
bus's location. Shared EU oil therefore selects DK2 equipment correctly. Raw-fuel
transport/imports, refining, storage and downstream accounting are excluded.
Biomass recognises raw bus carriers `biomass` and `solid biomass`; biogas is not
included. CHP is reduced once at its common fuel input. Stored fuel cannot bypass
unavailable equipment. Actual consumption need not fall by the same percentage.

## Sophia workflow

Build/solve the existing three source networks and prepare their weather donors
using the sector/weather workflows first. The campaign never schedules those
builds automatically.

Preview and check baselines:

```bash
python -m scripts.run_disruption_campaign --campaign baseline --variant all --list
python -m scripts.run_disruption_campaign --campaign baseline --variant all --check-inputs
sbatch run_disruption_baselines.sbatch
```

The baseline array has 15 tasks (`0-14%4`), ordered variant, then weather year.
It prepares and solves all baselines and writes all timing tables. Do not start
disruptions until these jobs have completed successfully and the windows have
been reviewed.

Then, for example:

```bash
python -m scripts.run_disruption_campaign --campaign fuel --variant 2025 --check-inputs
python -m scripts.run_disruption_campaign --campaign fuel --variant 2025 --write-plan results/disruption_campaign_v2/fuel_2025_tasks.csv
sbatch run_fuel_2025.sbatch
sbatch run_interconnector_2025.sbatch
```

Equivalent files exist for `2035_wo_V2G` and `2035_w_V2G`. Each interconnector
array has 175 tasks (`0-174%4`); each fuel array 225 (`0-224%4`). The list/plan
uses the exact same stable ordering as the array runner. `--write-plan` requires
completed baseline inputs and includes selected dates and output paths.

Change batching/concurrency without editing configs:

```bash
sbatch --array=0-49%2 run_fuel_2025.sbatch
sbatch --array=50-99%4 run_fuel_2025.sbatch
sbatch --array=100-224%4 run_fuel_2025.sbatch
sbatch --array=37 run_fuel_2025.sbatch  # retry one task
```

`--years` and `--cases` filter the task list; recalculate the array range from
`--list` if using them. `--task-index N --dry-run` previews one task. Each job owns
its outputs; disruption jobs are forbidden from building shared baselines. The
runner retains Snakemake's normal code/config/input rerun checks. Input checks reject
incomplete or stale baselines, changed shared policies and stale timing tables. A changed common
policy requires rerunning baselines before disruption jobs. Do not combine
simultaneous baseline rebuilds with running disruptions.

## Outputs and plotting

Shared per variant/weather: `network_prepared.nc`, baseline results,
`event_windows.csv`, `residual_load_hourly.csv`, and the reference MSV summary and
profiles once. Household shares remain in their original variant-specific input
CSV and network metadata; no per-case copies are written.

Each solved run retains `network_solved.nc`, `dispatch_summary.csv`,
`window_summary.csv`, `resource_usage.csv`, `run_info.json`, `status.json`, and
logs. Disruptions also retain `network_disrupted.nc` and `applied_disruption.csv`.
Hourly fuel/H2 reports can be regenerated using the existing report helpers and
the solved network; empty backup and upstream-supply reports are removed.

Detailed plots are opt-in and use the actual selected event, including one-hour
events, with exclusive end bounds:

```bash
snakemake plot_disruptions --cores 1 --allowed-rules plot_outage_dispatch plot_disruptions --configfiles config/config.sector_2025.yaml config/config.disruption_common.yaml config/config.fuel_disruption.yaml --config disruption_variant=2025 disruption_weather_year=2013-2014 disruption_case=gas_100pct_168h
```

After the complete campaign, make one shared metrics table and regional shedding
heatmaps for all variants/weather years:

```bash
snakemake summarise_disruptions --cores 1 --allowed-rules summarise_disruptions --configfiles config/config.sector_2025.yaml config/config.disruption_common.yaml config/config.fuel_disruption.yaml
```

Use the interconnector config instead for its summary. These rules only read
existing networks; missing campaign cells cause an error rather than silently
producing a partial comparison. `plots/metrics.csv` contains event/year metrics,
matching baseline values and differences. Full-year system costs/emissions come
from dispatch summaries; event-period system costs are not approximated from
partial rolling-window totals. The campaign does not export a CSV for each figure.
Historical standalone exploratory plot scripts remain for old saved experiments;
their former configurations can be recovered from Git history.

## Failure diagnostics

One live `status.json` records current stage/window/date bounds and last committed
window. Successful windows append to `window_summary.csv`; only the latest solver
log is kept. No per-window storage snapshots or separate ledgers are written on
success. `run_info.json` records effective settings, source paths, Git revision,
working-tree modification status, job identity and applied event.

On failure, `failures/<attempt>/` preserves `failure.txt`, the solver log if
available, opening storage states and remaining resource allowances. Retries
preserve earlier failures. Infeasible Gurobi solves get a conflict search limited
to 60 seconds and one readable `infeasibility.txt` with named constraints and
solver bounds. An incomplete search is explicitly reported. Time limits/numerical
errors are not diagnosed as infeasibility. `save_failed_model: true` optionally
adds one compressed full Gurobi model export; no large model dump is saved by
default. Hard job kills may leave only status and Sophia stdout/stderr.

Old test overlays, experiment-specific launchers, upstream fuel-disruption code,
and compatibility wrappers have been retired. Existing result folders remain
unchanged; historical assumptions remain in Git history.
