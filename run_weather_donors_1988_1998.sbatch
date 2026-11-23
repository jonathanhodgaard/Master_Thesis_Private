#!/bin/bash
#SBATCH --job-name=weather_1988_1998
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

# Override with sbatch --export=ALL,WEATHER_YEARS=1988_89 ...
# WEATHER_SCENARIOS accepts sector_2025, sector_2035_w_V2G,
# sector_2035_wo_V2G, or a space-separated selection of these.
read -r -a years <<< "${WEATHER_YEARS:-1988_89 1998_99}"
read -r -a scenarios <<< "${WEATHER_SCENARIOS:-sector_2025}"

# Validate every input before starting the expensive preparation stage.
for year in "${years[@]}"; do
  case "$year" in
    1988_89) cutout=europe-1986-1991-era5 ;;
    1998_99) cutout=europe-1996-2001-era5 ;;
    *) echo "Unsupported weather year: $year" >&2; exit 1 ;;
  esac
  path="data/cutout/archive/v1.0/${cutout}.nc"
  if [[ ! -f "$path" || ! -r "$path" ]]; then
    echo "Missing or unreadable archived cutout: $path" >&2
    echo "Run bash scripts/prepare_weather_1988_1998_links.sh first." >&2
    exit 1
  fi
  test -r "config/config.weather_${year}.yaml"
done
for scenario in "${scenarios[@]}"; do
  case "$scenario" in
    sector_2025|sector_2035_w_V2G|sector_2035_wo_V2G) ;;
    *) echo "Unsupported scenario: $scenario" >&2; exit 1 ;;
  esac
  test -r "config/config.${scenario}.yaml"
done

test -w /scratch
task_tmp_dir=$(mktemp -d "/scratch/${USER}-weather8898-${SLURM_JOB_ID}-XXXXXX")
export TMPDIR="$task_tmp_dir" TMP="$task_tmp_dir" TEMP="$task_tmp_dir"

# Sequential builds: preprocessing shares intermediate inputs.
# Request only prepared resources, stopping before capacity optimisation.
for year in "${years[@]}"; do
  for scenario in "${scenarios[@]}"; do
    case "$scenario" in
      sector_2025) horizon=2025 ;;
      sector_2035_*) horizon=2035 ;;
    esac
    target="resources/weather_${year}/${scenario}/networks/base_s_10___${horizon}.nc"
    echo "Preparing $year / $scenario -> $target"
    snakemake "$target" \
      --executor local --cores "$SLURM_CPUS_PER_TASK" \
      --resources mem_mb=120000 --default-resources "tmpdir=$TMPDIR" \
      --rerun-incomplete --printshellcmds \
      --configfiles "config/config.${scenario}.yaml" config/config.sophia.yaml \
                    "config/config.weather_${year}.yaml" "$@"
    # A dry-run previews targets without creating them.
    preview=false
    for argument in "$@"; do
      case "$argument" in -n|--dry-run|--dryrun) preview=true ;; esac
    done
    if [[ "$preview" == false ]]; then
      test -s "$target"
      echo "Prepared: $target"
    fi
  done
done
