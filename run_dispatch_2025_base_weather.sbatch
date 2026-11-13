#!/bin/bash
#SBATCH --job-name=dispatch_2025_base_weather
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
test -f results/sector_2025/networks/base_s_10___2025.nc
test -f results/sector_2025/csvs/household_heat_shares_base_s_10___2025.csv

# No donor network is needed: retain all operating inputs in the solved base.
python -c "import yaml; c=yaml.safe_load(open('config/config.disruption_dispatch_2025.yaml')); case=c['disruption']['cases'].get('base_weather'); assert isinstance(case, dict) and case.get('weather_network') is None, 'Enable base_weather with weather_network: null in the disruption configuration'"

# Explicit outputs select ONLY base_weather, even if other cases are enabled.
snakemake \
  results/disruptions/sector_2025_rolling/base_weather/network_solved.nc \
  results/disruptions/sector_2025_rolling/base_weather/household_heat_shares.csv \
  results/disruptions/sector_2025_rolling/base_weather/fuel_backup_dispatch.csv \
  results/disruptions/sector_2025_rolling/base_weather/resource_usage.csv \
  results/disruptions/sector_2025_rolling/base_weather/storage_msv.csv \
  results/disruptions/sector_2025_rolling/base_weather/window_summary.csv \
  results/disruptions/sector_2025_rolling/base_weather/dispatch_summary.csv \
  --executor local \
  --allowed-rules prepare_disruption_network solve_disruption_dispatch \
  --configfiles config/config.sector_2025.yaml \
                config/config.sophia.yaml \
                config/config.disruption_dispatch_2025.yaml \
  --cores "$SLURM_CPUS_PER_TASK" \
  --resources mem_mb=64000 \
  --default-resources "tmpdir=$TMPDIR" \
  --printshellcmds \
  "$@"
