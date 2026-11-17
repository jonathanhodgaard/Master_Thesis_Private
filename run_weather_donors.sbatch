#!/bin/bash
#SBATCH --job-name=weather_donors
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
task_tmp_dir=$(mktemp -d "/scratch/${USER}-weather-donors-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# Require existing archived cutouts (regular files or working symbolic links).
for cutout in europe-1961-1966-era5 europe-1966-1971-era5 europe-1981-1986-era5; do
  path="data/cutout/archive/v1.0/${cutout}.nc"
  if [[ ! -f "$path" || ! -r "$path" ]]; then
    echo "Missing or unreadable archived cutout: $path" >&2
    exit 1
  fi
done

# Sequential invocations avoid concurrent writes to shared preprocessing files.
# Explicit resources/ targets stop before solve_sector_network; no new capacity
# optimisation is needed to supply weather profiles to fixed-capacity dispatch.
for year in 1964_65 1965_66 1968_69 1982_83; do
  target="resources/weather_${year}/sector_2025/networks/base_s_10___2025.nc"
  echo "Preparing donor: $year -> $target"
  snakemake "$target" \
    --executor local \
    --cores "$SLURM_CPUS_PER_TASK" \
    --resources mem_mb=120000 \
    --default-resources "tmpdir=$TMPDIR" \
    --rerun-incomplete \
    --printshellcmds \
    --configfiles config/config.sector_2025.yaml \
                  config/config.sophia.yaml \
                  "config/config.weather_${year}.yaml" \
    "$@"
done

python -m scripts.run_disruption_campaign --check-inputs
