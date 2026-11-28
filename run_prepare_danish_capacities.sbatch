#!/bin/bash
#SBATCH --job-name=danish_capacities
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"
# Shared inputs: complete this step before submitting reference/weather jobs.
python -m snakemake prepare_danish_capacities --executor local \
  --cores "$SLURM_CPUS_PER_TASK" --rerun-incomplete --printshellcmds \
  --allowed-rules prepare_danish_capacities prepare_danish_power_capacities \
                  prepare_danish_heat_capacities prepare_danish_rural_heat \
  --configfiles config/config.sector_2025.yaml config/config.sophia.yaml "$@"
