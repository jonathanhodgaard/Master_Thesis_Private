#!/bin/bash
#SBATCH --job-name=dispatch_2025_disruptions
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
python - <<'PY'
from pathlib import Path
import yaml

with open("config/config.disruption_dispatch.yaml", encoding="utf-8") as stream:
    disruption = yaml.safe_load(stream)["disruption"]
inputs = [disruption["base_network"], disruption.get("household_shares")]
inputs += [case.get("weather_network") for case in disruption["cases"].values()]
for name in inputs:
    if name and not Path(name).is_file():
        raise FileNotFoundError(f"Required prepared/solved input is missing: {name}")
PY

# Run every configured case, including the baseline and interconnector outage.
# Restrict rules so external inputs cannot trigger investment/weather builds.
snakemake dispatch_disruptions \
  --executor local \
  --allowed-rules dispatch_disruptions prepare_disruption_network apply_interconnector_disruption solve_disruption_dispatch \
  --configfiles config/config.sector_2025.yaml \
                config/config.sophia.yaml \
                config/config.disruption_dispatch.yaml \
  --cores "$SLURM_CPUS_PER_TASK" \
  --resources mem_mb=64000 \
  --default-resources "tmpdir=$TMPDIR" \
  --printshellcmds \
  "$@"
