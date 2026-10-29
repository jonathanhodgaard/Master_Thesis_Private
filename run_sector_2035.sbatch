#!/bin/bash
#SBATCH --job-name=sector2035
#SBATCH --partition=workq
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --time=12:00:00
#SBATCH --chdir=/work/users/s203684/projects/Master_Thesis_Private
#SBATCH --output=logs/sector2035-%j.out
#SBATCH --error=logs/sector2035-%j.err

set -euo pipefail

cd /work/users/s203684/projects/Master_Thesis_Private
source /home/s203684/miniforge3/etc/profile.d/conda.sh
conda activate /work/users/s203684/projects/Master_Thesis_Private/.conda-env

echo "Running on $(hostname)"
echo "Repository: $PWD"
echo "Environment: $CONDA_PREFIX"

test -f config/config.sector_2035.yaml
test -w /scratch
python -c "import gurobipy as gp; m = gp.Model(); print('Gurobi license OK'); m.dispose()"

snakemake --executor local \
  --cores "$SLURM_CPUS_PER_TASK" \
  --resources mem_mb=64000 \
  --printshellcmds \
  results/sector_2035/networks/base_s_10___2035.nc \
  --configfile config/config.sector_2035.yaml config/config.sophia.yaml
