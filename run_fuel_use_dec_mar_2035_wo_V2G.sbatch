#!/bin/bash
#SBATCH --job-name=fuel_dec_mar_2035_wo_V2G
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=12:00:00
#SBATCH --array=0-1%2
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%A_%a.out
#SBATCH --error=logs/%x-%A_%a.err

set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_ARRAY_JOB_ID:-local}-${SLURM_ARRAY_TASK_ID:-check}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# Two weather-year tasks: 0=1965-1966, 1=1968-1969; gas_reduction_90 only.
# Simulate Dec 1-Apr 1 (exclusive); 90% gas reduction throughout that period.
# Process H2 penalty 200000 EUR/MWh. Independent output directories.
# Preflight: bash this_file.sbatch --list (or --check-inputs).
python -m scripts.run_disruption_campaign \
  --configfiles config/config.sector_2035_wo_V2G.yaml \
                config/config.sophia.yaml \
                config/config.fuel_use_disruption_2025.yaml \
                config/config.fuel_use_dec_mar_2035_wo_V2G.yaml \
  --years 1965-1966,1968-1969 \
  --cases gas_reduction_90 \
  --rerun-incomplete "$@"
