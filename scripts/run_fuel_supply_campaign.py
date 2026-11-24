"""Compatibility entry point for 2025 gas/oil CONSUMING-LINK cases.

Other arguments are the shared runner's --list, --check-inputs, --years,
--cases, --task-index, --run-year, --cores, --dry-run and --configfiles.
Use --sophia on the cluster to include its scratch-directory solver settings.
"""
import argparse
import sys

from scripts.run_disruption_campaign import main as run_campaign


def configfiles(full_year=False, sophia=False):
    paths = ['config/config.sector_2025.yaml']
    if sophia:
        paths.append('config/config.sophia.yaml')
    paths.append('config/config.fuel_use_disruption_2025.yaml')
    if not full_year:
        paths.append('config/config.fuel_use_winter_test_2025.yaml')
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--full-year', action='store_true')
    parser.add_argument('--sophia', action='store_true')
    args, remaining = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    run_campaign(remaining, default_configfiles=configfiles(args.full_year, args.sophia))


if __name__ == '__main__':
    main()
