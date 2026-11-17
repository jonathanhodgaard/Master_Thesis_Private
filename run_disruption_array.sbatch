#!/bin/bash
#SBATCH --job-name=interconnector
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=12:00:00
#SBATCH --array=0-31%4
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%A_%a.out
#SBATCH --error=logs/%x-%A_%a.err

set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# Four new years x eight cases, at most four independent nodes concurrently.
# Each task owns its case outputs; rolling windows inside it remain sequential.
# Change --array when changing --years/--cases. Run --list and --check-inputs first.
python -m scripts.run_disruption_campaign "$@"
