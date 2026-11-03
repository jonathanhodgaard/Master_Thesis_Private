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

echo "Running on $(hostname)"
echo "Environment: $CONDA_PREFIX"

python -c "import gurobipy as gp; m = gp.Model(); print('Gurobi license OK'); m.dispose()"
test -w /scratch

snakemake --executor local \
  --cores "$SLURM_CPUS_PER_TASK" \
  --printshellcmds \
  results/sector_2025_fix/networks/base_s_10___2025.nc \
  --configfile config/config.sector_2025.yaml config/config.sophia.yaml
