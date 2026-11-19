#!/bin/bash
#SBATCH --job-name=weather_2035_donors
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
task_tmp_dir=$(mktemp -d "/scratch/${USER}-weather2035-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# Build sequentially: preprocessing shares intermediate inputs.
# Select one year with sbatch --export=ALL,WEATHER_YEARS=1964_65 ...
for year in ${WEATHER_YEARS:-1964_65 1965_66 1968_69 1982_83}; do
  case "$year" in
    1964_65|1965_66) cutout=europe-1961-1966-era5 ;;
    1968_69) cutout=europe-1966-1971-era5 ;;
    1982_83) cutout=europe-1981-1986-era5 ;;
    *) echo "Unsupported weather year: $year" >&2; exit 1 ;;
  esac
  test -r "data/cutout/archive/v1.0/${cutout}.nc"
  for variant in w_V2G wo_V2G; do
    scenario="sector_2035_${variant}"
    snakemake "resources/weather_${year}/${scenario}/networks/base_s_10___2035.nc" \
      --executor local --cores "$SLURM_CPUS_PER_TASK" \
      --resources mem_mb=120000 --default-resources "tmpdir=$TMPDIR" \
      --rerun-incomplete --printshellcmds \
      --configfiles "config/config.${scenario}.yaml" config/config.sophia.yaml \
                    "config/config.weather_${year}.yaml" "$@"
  done
done
