#!/bin/bash
#SBATCH --job-name=dispatch_2035_wo_V2G
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=48:00:00
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# Exactly one weather year per submission; original weather runs first.
# Eight cases run sequentially, stopping at the first failure (IIS retained).
# Pass --years 1964-1965, etc. for subsequent submissions.
configfiles=(config/config.sector_2035_wo_V2G.yaml
             config/config.sophia.yaml
             config/config.interconnector_disruption.yaml
             config/config.interconnector_disruption_2035_wo_V2G.yaml)
if [[ ${NATIVE_LOW_VOLTAGE:-0} == 1 ]]; then
  configfiles+=(config/config.dispatch_2035_wo_V2G_native_low_voltage.yaml)
fi
python -m scripts.run_disruption_campaign \
  --configfiles "${configfiles[@]}" \
  --years 2013-2014 --run-year "$@"
