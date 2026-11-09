#!/bin/bash
#SBATCH --job-name=sector_2035_w_V2G
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

cd /work/users/s203684/projects/Master_Thesis_Private
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate /work/users/s203684/projects/Master_Thesis_Private/.conda-env

echo "Running on $(hostname)"
echo "Repository: $PWD"
echo "Environment: $CONDA_PREFIX"

test -f config/config.sector_2035_w_V2G.yaml
# Sophia's /tmp uses memory; keep large temporary files on node-local disk.
test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir"
export TMP="$task_tmp_dir"
export TEMP="$task_tmp_dir"
echo "Temporary directory: $TMPDIR"
python -c "import tempfile; print('Python temporary directory:', tempfile.gettempdir())"
df -h "$TMPDIR"

python -c "import gurobipy as gp; m = gp.Model(); print('Gurobi license OK'); m.dispose()"

snakemake --executor local \
  --cores "$SLURM_CPUS_PER_TASK" \
  --resources mem_mb=64000 \
  --default-resources "tmpdir=$TMPDIR" \
  --printshellcmds \
  results/sector_2035_w_V2G/networks/base_s_10___2035.nc \
  --configfile config/config.sector_2035_w_V2G.yaml config/config.sophia.yaml
