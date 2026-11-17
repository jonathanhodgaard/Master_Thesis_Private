# Interconnector disruption across weather years on Sophia

The campaign config is `config/config.interconnector_disruption.yaml`. It contains
the reference weather (2013-2014) and four additional July-June weather years.
One workflow invocation selects one year; the Slurm array covers all four new
years and all eight interconnector cases, including the matching no-outage control.
The investment capacities and scarcity-price reference stay fixed at the solved
2025 base. A donor supplies operating profiles, not new investment capacities.

## Weather-year selection and Gøtske

Gøtske et al. (2024), *Designing a sector-coupled European energy system robust to
60 years of historical weather data*, DOI: https://doi.org/10.1038/s41467-024-54853-3,
uses **calendar years**. The main text on pages 3-4 and Fig. 2 identify January
1966 and December 1968 as major loss-of-load periods. Therefore:

| July-June donor | Interpretation |
| --- | --- |
| 1965-1966 | Confirmed stress selection: includes the January 1966 wind/cold event. |
| 1968-1969 | Confirmed stress selection: includes the December 1968 wind drought. |
| 1964-1965 | Requested comparison year; not explicitly designated a “good year” in the main text. |
| 1982-1983 | Requested comparison year; not explicitly designated a “good year” in the main text. |

The paper does not establish this exact four-year set as a ranked two-good/two-bad
sample. Its European net-zero system also differs from this seven-country 2025
system and from an isolated Eastern Denmark. Treat the latter two as comparison
years until their no-outage controls establish their severity in this model.
Compare unserved energy and operating costs, including DK2-specific results;
annual wind or heating-demand averages alone do not establish adequacy.
Do not confuse robust *design-year capacities* with easy *operating weather*.

In Fig. 2b, the horizontal axis is the **design year**, sorted by the average peak
loss of load of that year's capacity layout across operating weather years. The
years on the right identify less robust capacity layouts. They do not identify
the most difficult operating weather years. Each red point instead represents an
operating year applied to that layout. Testing the right-hand design years would
require building their capacity layouts; swapping donor weather into one fixed
2025 layout answers a different question.

The code transfers donor profiles by matching month/day/hour and **retains the
base network's July 2013-June 2014 timestamps**. Keep outage dates, zoom windows,
and simulation cutoffs on that reference calendar. January 2014 then represents
January 1966 in the 1965-1966 campaign, and December 2013 represents December
1968 in the 1968-1969 campaign. Leap days must be removed from donors.

## Output folders

```text
results/disruptions/sector_2025_interconnector_disruption/
  1965-1966/
    base_weather/
    se_interconnector/
      network_prepared.nc
      network_disrupted.nc
      network_solved.nc
      dispatch_summary.csv
      window_summary.csv
      solver_window_0000.log
      ... other reports and rolling-window logs ...
      plots/
    ... remaining interconnector cases ...
    plots/  # comparisons of all cases in this weather year
```

Cases sit directly under each weather year. Rolling-window reports and logs stay
inside their interconnector case. For a separate experiment with changed outage
dates or assumptions, use a different `disruption.output_directory` and preserve
its config. Existing result folders are not moved. The legacy local plotting
overlay disables the weather-year layout so older saved networks remain usable.

## Prepare donor networks first

The config lists the required donor paths explicitly. They must already exist on
Sophia and match the base's asset names, hourly calendar and snapshot weights.
The existing `config.weather_1965_66.yaml` is retained. Additional build overlays
are supplied for 1964-1965, 1968-1969 and 1982-1983. Load them **only when building
donors**, after the sector/Sophia configs, without the disruption config.

For example, preview the 1964-1965 donor preprocessing:

```bash
snakemake --dry-run --cores 1 \
  resources/weather_1964_65/sector_2025/networks/base_s_10___2025.nc \
  --configfiles config/config.sector_2025.yaml config/config.sophia.yaml \
                config/config.weather_1964_65.yaml
```

Repeat for the other year-specific overlays and targets. Run actual preprocessing
in a compute allocation, and finish donor builds before starting the dispatch
array: different builds can share intermediate inputs. All four overlays use
archived cutouts (`source: archive`, `version: v1.0`):

| Weather period | Cutout under `data/cutout/archive/v1.0/` |
| --- | --- |
| 1964-1965 | `europe-1961-1966-era5.nc` |
| 1965-1966 | `europe-1961-1966-era5.nc` |
| 1968-1969 | `europe-1966-1971-era5.nc` |
| 1982-1983 | `europe-1981-1986-era5.nc` |

On Sophia these files can be symbolic links to `/groups/EXTREMES/cutouts/`.
The snapshot settings select the July-June simulation period within each cutout;
the cutout itself covers the full multi-year archive. With valid links in place,
these overlays use the existing weather data without building new ERA5 cutouts.
The cutouts are weather inputs, not the prepared donor network files: donor
network preprocessing is still required. Full donor builds have not been executed
locally.

### Build all four donors in one Sophia batch job

The disruption launcher does **not** build missing donors: it checks for them
first and restricts workflow rules to dispatch and plotting. Use
`run_weather_donors.sbatch` for the separate preprocessing stage. It builds the
four explicit `resources/.../networks/base_s_10___2025.nc` targets sequentially,
without requesting investment optimisation. It checks the three archived cutouts
before starting and checks all dispatch inputs after the builds finish.

With the updated files on Sophia, preview all four from the repository:

```bash
cd /work/users/s203684/projects/Master_Thesis_Private
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"
mkdir -p logs
for year in 1964_65 1965_66 1968_69 1982_83; do
  snakemake --dry-run --cores 1 \
    "resources/weather_${year}/sector_2025/networks/base_s_10___2025.nc" \
    --configfiles config/config.sector_2025.yaml config/config.sophia.yaml \
                  "config/config.weather_${year}.yaml" || break
done
bash -n run_weather_donors.sbatch
```

Submit preprocessing, then check the batch logs and completion status:

```bash
sbatch run_weather_donors.sbatch
```

The script requests one exclusive workq node, 32 CPUs, and 48 hours, following the
existing sector preprocessing allocation. Adjust wall time to observed runtime.
Do not run another build writing the same preprocessing files concurrently.
After successful completion, use the dispatch submission commands below. To queue
dispatch automatically after a successful donor job, submit with a dependency:

```bash
donor_job=$(sbatch --parsable run_weather_donors.sbatch) && \
  sbatch --dependency=afterok:"${donor_job%%;*}" run_disruption_array.sbatch
```

Use either this dependent pair or the manual submission above, not both. The
dependent dispatch array runs only if the donor batch finishes successfully,
including its final dispatch-input check.

## Submit the four new years

From the Sophia repository with the existing environment activated:

```bash
mkdir -p logs
python -m scripts.run_disruption_campaign --list
python -m scripts.run_disruption_campaign --check-inputs
python -m scripts.run_disruption_campaign --task-index 0 --cores 32 --dry-run
bash -n run_disruption_array.sbatch
sbatch run_disruption_array.sbatch
```

The array is `0-31%4`: 32 tasks, up to four concurrently, each requesting one
exclusive workq node with 32 CPUs and a 12-hour limit. IDs 0-7 use 1964-1965,
8-15 use 1965-1966, 16-23 use 1968-1969, and 24-31 use 1982-1983, with case order
printed by `--list`. Adjust the time request based on observed full-year case
runtime. The inherited Gurobi setting remains eight threads per solve; requesting
an exclusive node provides memory isolation, not an automatic 32-thread speedup.
Benchmark solver thread counts before increasing them. The cap can be changed
without editing the script:

```bash
sbatch --array=0-31%2 run_disruption_array.sbatch
```

This distributes independent model runs across nodes. A single Gurobi solve is
not distributed across nodes, and rolling windows stay sequential because storage,
resource allowances and implemented dispatch carry forward between them.
The launcher checks each selected task's base, heat shares and donor before running;
`--check-inputs` checks the entire campaign before submission.

Each task requests its solved network and case plots, never shared comparison
plots. It restricts allowed workflow rules to dispatch and plotting, so a missing
external donor cannot silently start an investment/weather build. It uses
`--nolock` because outputs are disjoint between tasks. **Do not run two arrays or
another workflow targeting the same weather-year/case folders at once.**
Keep code, config, year ordering and case ordering unchanged while tasks are queued
or running; task IDs are resolved from those files at execution time.

Logs are `logs/interconnector-ARRAYID_TASKID.out` and `.err`. Inspect with
`squeue -r -u "$USER"` and
`sacct -j ARRAYID --format=JobID,State,ExitCode,Elapsed,MaxRSS`.
After fixing a failed task, resubmit only that index (example: 9):

```bash
sbatch --array=9 run_disruption_array.sbatch --rerun-incomplete
```

For a smaller selection, **change the array bounds to match `--list`**:

```bash
python -m scripts.run_disruption_campaign --years 1965-1966 --cases base_weather,se_interconnector --list
sbatch --array=0-1%2 run_disruption_array.sbatch --years 1965-1966 --cases base_weather,se_interconnector
```

To run the original reference-year campaign, `sbatch run_dispatch_2025.sbatch`
still runs its eight cases. That script also accepts `DISRUPTION_WEATHER_YEAR`,
`DISRUPTION_CASE` via `sbatch --export=ALL,...`.

## Comparison plots after the array

Run once per year after its cases finish. Restrict to plotting rules so this
cannot start solves, and omit `disruption_case` so the comparison includes all
eight cases. For example, inside an appropriate compute allocation:

```bash
snakemake plot_disruptions --cores 2 \
  --allowed-rules plot_outage_dispatch plot_outage_missing_demand \
  --rerun-triggers mtime \
  --configfiles config/config.sector_2025.yaml config/config.sophia.yaml \
                config/config.interconnector_disruption.yaml \
  --config disruption_weather_year=1965-1966
```

Comparison plots and their summary/hourly CSVs appear under that weather year's `plots/`.

References: [Sophia scheduler](https://dtu-sophia.github.io/docs/scheduler/) and
[Slurm job arrays](https://slurm.schedmd.com/job_array.html).
