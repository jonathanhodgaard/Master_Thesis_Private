#!/bin/bash
#SBATCH --job-name=weather_2019_2020
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

# Default: prepare all three models sequentially, avoiding shared-input races.
# Select one with sbatch --export=ALL,WEATHER_SCENARIOS=sector_2025 ...
read -r -a scenarios <<< "${WEATHER_SCENARIOS:-sector_2025 sector_2035_w_V2G sector_2035_wo_V2G}"
test -r data/cutout/archive/v1.0/europe-2016-2021-era5.nc
test -r config/config.weather_2019_20.yaml
test -r scripts/check_weather_donor.py
for scenario in "${scenarios[@]}"; do
  case "$scenario" in
    sector_2025|sector_2035_w_V2G|sector_2035_wo_V2G) ;;
    *) echo "Unsupported scenario: $scenario" >&2; exit 1 ;;
  esac
  test -r "config/config.${scenario}.yaml"
done

test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-weather1920-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"
preview=false
for argument in "$@"; do
  case "$argument" in -n|--dry-run|--dryrun) preview=true ;; esac
done

for scenario in "${scenarios[@]}"; do
  case "$scenario" in
    sector_2025) horizon=2025 ;;
    sector_2035_*) horizon=2035 ;;
  esac
  target="resources/weather_2019_20/${scenario}/networks/base_s_10___${horizon}.nc"
  echo "Preparing 2019-2020 / $scenario -> $target"
  snakemake "$target" \
    --executor local --cores "$SLURM_CPUS_PER_TASK" \
    --resources mem_mb=120000 --default-resources "tmpdir=$TMPDIR" \
    --rerun-incomplete --printshellcmds \
    --configfiles "config/config.${scenario}.yaml" config/config.sophia.yaml \
                  config/config.weather_2019_20.yaml "$@"
  if [[ "$preview" == false ]]; then
    test -s "$target"
    python -m scripts.check_weather_donor "$target" --start 2019-07-01 --end 2020-07-01
    echo "Prepared and validated: $target"
  fi
done
