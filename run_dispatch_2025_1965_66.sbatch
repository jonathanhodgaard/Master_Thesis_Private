#!/bin/bash
#SBATCH --job-name=dispatch_2025_1965_66
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

test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir"
export TMP="$task_tmp_dir"
export TEMP="$task_tmp_dir"

# Existing solved base, matching share CSV, and weather donor are required.
test -f results/sector_2025/networks/base_s_10___2025.nc
test -f results/sector_2025/csvs/household_heat_shares_base_s_10___2025.csv
test -f resources/weather_1965_66/sector_2025/networks/base_s_10___2025.nc

snakemake dispatch_disruptions \
  --executor local \
  --allowed-rules dispatch_disruptions prepare_disruption_network solve_disruption_dispatch \
  --configfiles config/config.sector_2025.yaml \
                config/config.sophia.yaml \
                config/config.disruption_dispatch_2025.yaml \
  --cores "$SLURM_CPUS_PER_TASK" \
  --resources mem_mb=64000 \
  --default-resources "tmpdir=$TMPDIR" \
  --printshellcmds
