#!/bin/bash
#SBATCH --job-name=sector2025
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

# ── Scenario settings: change these for each experiment ──────────────
CONFIG_FILES=(
    config/config.sector_2025.yaml
    config/config.sophia.yaml
)
TARGET="results/sector_2025_fix/networks/base_s_10___2025.nc"

# ── Environment ────────────────────────────────────────────────────
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate "$PWD/.conda-env"

# Check inputs before starting.
for config in "${CONFIG_FILES[@]}"; do
    test -f "$config" || {
        echo "Missing configuration: $config" >&2
        exit 1
    }
done

# ── Temporary storage on compute-node disk ──────────────────────────
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_JOB_ID}-XXXXXX")

export TMPDIR="$task_tmp_dir"
export TMP="$task_tmp_dir"
export TEMP="$task_tmp_dir"

# Remove temporary files on success; retain them on failure.
cleanup() {
    status=$?
    if (( status == 0 )); then
        rm -rf -- "$task_tmp_dir"
    else
        echo "Job failed; temporary files retained at: $task_tmp_dir" >&2
    fi
}
trap cleanup EXIT

# ── Diagnostics and solver check ───────────────────────────────────
echo "Started: $(date)"
echo "Host: $(hostname)"
echo "Repository: $PWD"
echo "Environment: $CONDA_PREFIX"
echo "CPUs: $SLURM_CPUS_PER_TASK"
echo "Temporary directory: $TMPDIR"
echo "Target: $TARGET"

df -h "$PWD" "$TMPDIR"

python -c "import gurobipy as gp; m = gp.Model(); print('Gurobi license OK'); m.dispose()"

# ── Run workflow ───────────────────────────────────────────────────
snakemake --executor local \
    --cores "$SLURM_CPUS_PER_TASK" \
    --default-resources "tmpdir=$TMPDIR" \
    --printshellcmds \
    --configfile "${CONFIG_FILES[@]}" \
    -- "$TARGET"

echo "Finished: $(date)"