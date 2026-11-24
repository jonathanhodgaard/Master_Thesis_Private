#!/bin/bash
#SBATCH --job-name=interconnector_2035_w_V2G
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=12:00:00
#SBATCH --array=0-39%4
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%A_%a.out
#SBATCH --error=logs/%x-%A_%a.err

set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# One full-year weather/case per independent array task. A failed task has no
# dependency on another task and cannot stop the rest of this array.
# Keep the year/case ordering fixed while the array is queued or running.
python -m scripts.run_disruption_campaign \
  --configfiles config/config.sector_2035_w_V2G.yaml \
                config/config.sophia.yaml \
                config/config.interconnector_disruption.yaml \
                config/config.interconnector_disruption_2035_w_V2G.yaml \
  --years 2013-2014,1965-1966,1968-1969,1988-1989,2019-2020 \
  --rerun-incomplete "$@"
