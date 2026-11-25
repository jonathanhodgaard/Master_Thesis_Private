#!/usr/bin/env bash
# Plot existing 2025 INTERCONNECTOR results only. No dispatch solves are allowed.
set -uo pipefail

usage() {
  cat <<'HELP'
Usage: bash scripts/run_interconnector_plots_2025.sh [--comparisons-only] [--dry-run]

Default: per-case dispatch/shares, cross-case missing demand, heatmaps and all six
standalone comparison tools, for the 2025 interconnector campaign only.
--comparisons-only skips individual dispatch/shares but keeps missing-demand comparisons.
--dry-run prints the exact commands without loading networks or creating plots.

Run from an activated project environment on an allocated compute node.
Optional environment variables: CORES=8, PLOT_PYTHON=python, LOG_DIR=logs/plots/2025,
YEARS="2013-2014 1965-1966 1968-1969 1988-1989 2019-2020".
YEARS selects per-year Snakemake steps; standalone/heatmap comparisons use config years.
Standalone tools run sequentially. No cross-planning-year comparison is run.
HELP
}

comparisons_only=false
dry_run=false
for arg in "$@"; do
  case "$arg" in
    --comparisons-only) comparisons_only=true ;;
    --dry-run) dry_run=true ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

# Installed in scripts/; resolve the repository root independently of caller cwd.
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || exit 2
cd -- "$repo_root" || exit 2
CORES=${CORES:-8}
PLOT_PYTHON=${PLOT_PYTHON:-python}
LOG_DIR=${LOG_DIR:-logs/plots/2025}
if [[ ! "$CORES" =~ ^[1-9][0-9]*$ ]]; then
  printf 'CORES must be a positive integer.\n' >&2
  exit 2
fi
read -r -a years <<< "${YEARS:-2013-2014 1965-1966 1968-1969 1988-1989 2019-2020}"
if ((${#years[@]} == 0)); then
  printf 'No weather years selected.\n' >&2
  exit 2
fi
for year in "${years[@]}"; do
  case "$year" in
    2013-2014|1965-1966|1968-1969|1988-1989|2019-2020) ;;
    *) printf 'Unsupported interconnector weather year: %s\n' "$year" >&2; exit 2 ;;
  esac
done

configs=(config/config.sector_2025.yaml config/config.sophia.yaml config/config.interconnector_disruption.yaml)
comparison_config=config/config.interconnector_disruption.yaml
tools=(plot_disruption_spread plot_spare_capacity plot_gas_store
       plot_price_duration plot_price_timeseries plot_aftershock)
for file in "${configs[@]}" rules/plot_disruption.smk; do
  if [[ ! -f "$file" ]]; then
    printf 'Required local file missing: %s\n' "$file" >&2; exit 2
  fi
done
for tool in "${tools[@]}"; do
  if [[ ! -f "scripts/$tool.py" ]]; then
    printf 'Required plotting script missing: scripts/%s.py\n' "$tool" >&2; exit 2
  fi
done
if ! "$dry_run"; then
  command -v snakemake >/dev/null || { printf 'Activate the project environment: snakemake is missing.\n' >&2; exit 2; }
  command -v "$PLOT_PYTHON" >/dev/null || { printf 'Python executable missing: %s\n' "$PLOT_PYTHON" >&2; exit 2; }
  mkdir -p -- "$LOG_DIR" || exit 2
fi

failed=()
run_logged() {
  local label=$1
  shift
  if "$dry_run"; then
    printf '[%s] ' "$label"
    printf '%q ' "$@"
    printf '\n'
    return 0
  fi
  printf 'Running %s; log: %s/%s.log\n' "$label" "$LOG_DIR" "$label"
  if "$@" > "$LOG_DIR/$label.log" 2>&1; then
    printf '  %s OK\n' "$label"
  else
    local status=$?
    printf '  %s FAILED (exit %s); see %s/%s.log\n' "$label" "$status" "$LOG_DIR" "$label" >&2
    failed+=("$label")
  fi
}

target=plot_disruptions
if "$comparisons_only"; then
  target=plot_outage_missing_demand
fi
for year in "${years[@]}"; do
  run_logged "snakemake_$year" snakemake "$target" --cores "$CORES" \
    --allowed-rules plot_disruptions plot_outage_dispatch plot_outage_missing_demand \
    --rerun-triggers mtime --configfiles "${configs[@]}" \
    --config "disruption_weather_year=$year" plot_existing_only=true
done

run_logged heatmaps snakemake plot_disruption_heatmaps --cores "$CORES" \
  --allowed-rules disruption_case_metrics plot_disruption_heatmaps \
  --rerun-triggers mtime --forcerun disruption_case_metrics \
  --configfiles "${configs[@]}"

for tool in "${tools[@]}"; do
  run_logged "$tool" "$PLOT_PYTHON" "scripts/$tool.py" "$comparison_config"
done

if ((${#failed[@]})); then
  printf '\nFailed plotting steps (%s):\n' "${#failed[@]}" >&2
  printf '  %s\n' "${failed[@]}" >&2
  exit 1
fi
if ! "$dry_run"; then
  printf '\n2025 plotting steps finished. Logs: %s\n' "$LOG_DIR"
fi
