#!/bin/bash
#SBATCH --job-name=dispatch_2025
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=12:00:00
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"

# Use compute-node disk rather than Sophia's memory-backed /tmp.
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir"
export TMP="$task_tmp_dir"
export TEMP="$task_tmp_dir"

test -f config/config.sophia.yaml

# The single dispatch config selects reference inputs, cases and rolling settings.
# Fail before scheduling jobs if an external solved input is missing.
dispatch_target=$(python - <<'PY'
from pathlib import Path
import os
import yaml
from scripts.disruption_scenarios import select_disruption

with open("config/config.interconnector_disruption.yaml", encoding="utf-8") as stream:
    config = yaml.safe_load(stream)
for variable, key in [("DISRUPTION_WEATHER_YEAR", "disruption_weather_year"),
                      ("DISRUPTION_CASE", "disruption_case")]:
    if os.environ.get(variable):
        config[key] = os.environ[variable]
disruption = select_disruption(config)
case = os.environ.get("DISRUPTION_CASE")
if case and case not in disruption["cases"]:
    raise ValueError(f"Unknown DISRUPTION_CASE {case!r}; choose from {list(disruption['cases'])}")
selected = [case] if case else list(disruption["cases"])
inputs = [disruption["base_network"], disruption.get("household_shares")]
inputs += [disruption["cases"][name].get("weather_network") for name in selected]
for name in inputs:
    if name and not Path(name).is_file():
        raise FileNotFoundError(f"Required prepared/solved input is missing: {name}")
print(f"{disruption['output_directory'].rstrip('/')}/{case}/network_solved.nc" if case else "dispatch_disruptions")
PY
)

config_overrides=()
if [[ -n ${DISRUPTION_WEATHER_YEAR:-} ]]; then
  config_overrides+=("disruption_weather_year=$DISRUPTION_WEATHER_YEAR")
fi
if [[ -n ${DISRUPTION_CASE:-} ]]; then
  config_overrides+=("disruption_case=$DISRUPTION_CASE")
fi
config_args=()
if (( ${#config_overrides[@]} )); then
  config_args=(--config "${config_overrides[@]}")
fi

# Run all configured cases, or select one with DISRUPTION_CASE=se_interconnector.
# Restrict rules so external inputs cannot trigger investment/weather builds.
snakemake "$dispatch_target" \
  --executor local \
  --allowed-rules dispatch_disruptions prepare_disruption_network apply_interconnector_disruption solve_disruption_dispatch \
                  plot_outage_dispatch plot_outage_missing_demand \
  --rerun-triggers mtime \
  --configfiles config/config.sector_2025.yaml \
                config/config.sophia.yaml \
                config/config.interconnector_disruption.yaml \
  --cores "$SLURM_CPUS_PER_TASK" \
  --resources mem_mb=64000 \
  --default-resources "tmpdir=$TMPDIR" \
  --printshellcmds \
  "${config_args[@]}" \
  "$@"
