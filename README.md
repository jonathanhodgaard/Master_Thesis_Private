<!--
SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
SPDX-License-Identifier: CC-BY-4.0
-->

# Danish thesis model: preparation, infrastructure references and disruptions

This repository extends [PyPSA-Eur v2026.08.0](https://github.com/PyPSA/pypsa-eur/releases/tag/v2026.08.0)
with Danish capacity preparation, sector-coupled infrastructure assumptions, and
fixed-capacity rolling disruption studies. It contains the active preparation
scripts and their source snapshots; the `master_thesis` sibling is no longer a
runtime dependency.

Follow **[Preparation and execution workflow](doc/preparation_workflow.md)** in
this order:

1. **Environment and sources.** Install the project environment. Normal PyPSA-Eur
   rules retrieve upstream inputs as needed; the pinned Danish register/JRC/PPM
   snapshots are included under `data/danish_capacity_sources/`.
2. **Prepare our data.** Generate electricity, CHP/district heat, rural/decentral
   heat and rooftop-PV tables. Preparation is offline and shared across variants.
3. **Prepare networks.** Run normal electricity/sector construction using those
   tables, rather than modifying downloaded archive files manually.
4. **Solve infrastructure references.** Co-optimise capacities and operation over
   the full year for 2025, 2035 without V2G, and 2035 with V2G. These use operational
   perfect foresight and the PyPSA-Eur `foresight: overnight` workflow.
5. **Prepare weather donors.** Build the matching weather-dependent operating
   inputs for the selected historical years.
6. **Run rolling baselines.** Freeze the selected infrastructure and solve the
   15 variant/weather controls with a 168-hour horizon and 48-hour overlap.
7. **Select events and run disruptions.** Select two sets of five DK2 windows:
   total electricity demand minus available wind/solar for interconnectors, and
   useful heat demand minus available wind/solar for fuels. Heating electricity
   stays in the electrical criterion. Run 525 interconnector and 675 DK2 fuel-use
   cases using their respective dates.
8. **Compare and plot.** Use each case's matching rolling baseline.

Snakemake expresses this dependency order. Unrelated downloads do not all need to
finish before a particular Danish preparation rule can run.

## Start with Danish data preparation

From the repository root:

```bash
pixi install
pixi run snakemake prepare_danish_capacities --cores 1 --configfiles config/config.sector_2025.yaml
```

On Sophia, prepare shared inputs with `sbatch run_prepare_danish_capacities.sbatch`
and wait for completion before reference/weather builds. Source files and
checksums are documented in [Danish source snapshots](data/danish_capacity_sources/README.md).
No postcode API key is needed for the supplied snapshots.

The prepared powerplant table is **`data/existing_infrastructure/powerplants.csv`**.
All three active sector configs select it through `electricity.prepared_powerplants`.
Setting this option to `null` retains the standard PyPSA-Eur powerplant retrieval
branch. Downloaded archives are never outputs of our preparation rules.

## Build a full-year infrastructure reference

After reviewing the prepared tables:

```bash
pixi run snakemake results/sector_2025/networks/base_s_10___2025.nc --cores 4 --configfiles config/config.sector_2025.yaml
```

The detailed guide gives preparation-only targets, both 2035 variants, and Sophia
submission commands. Existing reference targets may be rebuilt when explicitly
requested after input changes; use a distinct run name/path to retain an older
study. This migration did not rebuild or modify any saved networks/results.
The previous private powerplant input is retained intact under
`data/existing_infrastructure/archive/powerplants_pre_migration.csv`.
The migrated recipe reproduces the current companion output, which differs from
that older input; see [migration comparison](doc/danish_capacity_migration.json).

## Continue with the rolling disruption campaign

After references and donors exist, follow **[Disruption campaign](doc/disruption_campaign.md)**.
The two campaign configs share `config/config.disruption_common.yaml`.
New campaign outputs use `results/disruption_campaign_v2/`.

```bash
sbatch run_disruption_baselines.sbatch
```

Review the generated event windows before submitting
`run_interconnector_<variant>.sbatch` or `run_fuel_<variant>.sbatch`.
The campaign guide covers input checks, array subsets/concurrency, compact
outputs, shared plotting metrics, and failure diagnostics.

## Model documentation

- [Preparation and execution workflow](doc/preparation_workflow.md): active inputs,
  preparation rules, assumptions and execution order.
- [Disruption campaign](doc/disruption_campaign.md): rolling controls, timing,
  disruption definitions and reporting.
- [Thesis changes](doc/thesis_changes.md): historical extension details; older
  experiment commands are superseded by the active guides above.
- [PyPSA-Eur documentation](https://pypsa-eur.readthedocs.io/): upstream model,
  retrieval, configuration and limitations.

Perfect-foresight **infrastructure references** and rolling **no-disruption
baselines** are different stages. Use these terms consistently when documenting
experiments. Older overlays and the companion project's early fuel-price-shock
workflow are historical material, not part of the active campaign.
