#!/bin/bash
#SBATCH --job-name=sector2025
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=48:00:00
#SBATCH --output=logs/sector2025-%j.out
#SBATCH --error=logs/sector2025-%j.err

set -euo pipefail

cd /work/users/s203684/projects/Master_Thesis_Private
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"

# Use compute-node disk rather than the memory-backed /tmp filesystem.
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-sector2025-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir"
export TMP="$task_tmp_dir"
export TEMP="$task_tmp_dir"

echo "Running on $(hostname)"
echo "Environment: $CONDA_PREFIX"
python -c "import tempfile; print('Python temporary directory:', tempfile.gettempdir())"
df -h "$PWD" "$TMPDIR"
df -i "$PWD" "$TMPDIR"

python -c "import gurobipy as gp; m = gp.Model(); print('Gurobi license OK'); m.dispose()"

snakemake --executor local \
  --cores "$SLURM_CPUS_PER_TASK" \
  --default-resources tmpdir="$TMPDIR" \
  --printshellcmds \
  results/sector_2025/networks/base_s_10___2025.nc \
  --configfile config/config.sector_2025.yaml config/config.sophia.yaml
