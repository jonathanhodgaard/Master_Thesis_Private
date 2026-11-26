#!/bin/bash
# Shared Sophia environment; scenario definitions live in the two campaign configs.
set -euo pipefail
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"

# Listing/checking is read-only and does not need job scratch space.
for argument in "$@"; do
  case "$argument" in
    --list|--check-inputs|--write-plan)
      exec python -m scripts.run_disruption_campaign --sophia "$@"
      ;;
  esac
done

test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_ARRAY_JOB_ID:-local}-${SLURM_ARRAY_TASK_ID:-check}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"
exec python -m scripts.run_disruption_campaign --sophia "$@"
