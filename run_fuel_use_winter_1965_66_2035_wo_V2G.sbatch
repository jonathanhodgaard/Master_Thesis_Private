#!/bin/bash
#SBATCH --job-name=fuel_h2_200k_2035_wo_V2G_6566
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=12:00:00
#SBATCH --array=0-6%4
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%A_%a.out
#SBATCH --error=logs/%x-%A_%a.err

set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_ARRAY_JOB_ID:-local}-${SLURM_ARRAY_TASK_ID:-check}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# 1965/66 weather, Jan 1-29 model calendar; event Jan 8-22 (end-exclusive).
# Seven independent cases: control, gas 50/75/90%, oil 50/75/90% reductions.
# Process H2 penalty: 200000 EUR/MWh; separate h2_200000_test output directory.
# Final-demand shedding prices and production minima retain their existing settings.
# Preflight: bash this_file.sbatch --list (or --check-inputs).
# Keep years/cases fixed to match array 0-6; extra args pass to the runner.
python -m scripts.run_disruption_campaign \
  --configfiles config/config.sector_2035_wo_V2G.yaml \
                config/config.sophia.yaml \
                config/config.fuel_use_disruption_2025.yaml \
                config/config.fuel_use_winter_test_2025.yaml \
                config/config.fuel_use_winter_1965_66_2035_wo_V2G.yaml \
  --years 1965-1966 \
  --cases base_weather,gas_reduction_50,gas_reduction_75,gas_reduction_90,oil_reduction_50,oil_reduction_75,oil_reduction_90 \
  --rerun-incomplete "$@"
