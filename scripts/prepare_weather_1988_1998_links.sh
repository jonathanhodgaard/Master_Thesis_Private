#!/bin/bash
# Run from the repository root on Sophia; existing files are never replaced.
set -euo pipefail
cutout_dir=${CUTOUT_SOURCE_DIR:-/groups/EXTREMES/cutouts}
destination=data/cutout/archive/v1.0
mkdir -p "$destination"
for name in europe-1986-1991-era5.nc europe-1996-2001-era5.nc; do
  source_file="$cutout_dir/$name"
  link="$destination/$name"
  if [[ ! -f "$source_file" || ! -r "$source_file" ]]; then
    echo "Missing or unreadable source: $source_file" >&2
    exit 1
  fi
  if [[ -e "$link" || -L "$link" ]]; then
    if [[ ! "$link" -ef "$source_file" ]]; then
      echo "Existing destination points elsewhere; inspect before changing: $link" >&2
      ls -ld -- "$link" >&2
      exit 1
    fi
  else
    ln -s -- "$source_file" "$link"
  fi
  test -r "$link"
  ls -lhL -- "$link"
done
