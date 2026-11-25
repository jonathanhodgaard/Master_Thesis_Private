"""Plan and run independent Sophia array tasks (one weather year and case each)."""
import argparse
from copy import deepcopy
import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml

from scripts.disruption_scenarios import select_disruption


CONFIGFILES = ["config/config.sector_2025.yaml", "config/config.sophia.yaml",
               "config/config.interconnector_disruption.yaml"]
RULES = ["dispatch_disruptions", "prepare_disruption_network", "apply_interconnector_disruption",
         "solve_disruption_dispatch", "plot_outage_dispatch", "plot_outage_missing_demand"]
NEW_YEARS = ["1965-1966", "1968-1969", "1988-1989", "2019-2020"]


def merge_config(target, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_config(target[key], value)
        else:
            target[key] = deepcopy(value)


def load_config(paths):
    config = {}
    for path in paths:
        merge_config(config, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    return config


def campaign_tasks(config, years, cases=None):
    tasks = []
    for year in years:
        for case in cases or config["disruption"]["cases"]:
            selection = {"disruption_weather_year": year, "disruption_case": case}
            settings = select_disruption({**config, **selection})
            tasks.append({"year": year, "case": case, "selection": selection,
                          "settings": settings,
                          "target": f"{settings['output_directory']}/{case}/network_solved.nc"})
    targets = [task["target"] for task in tasks]
    if not tasks or len(set(targets)) != len(targets):
        raise ValueError("Campaign must have at least one task and no overlapping output paths.")
    return tasks


def check_inputs(tasks):
    paths = set()
    for task in tasks:
        settings = task["settings"]
        paths.update(filter(None, [settings["base_network"], settings.get("household_shares"),
                                 settings["cases"][task["case"]].get("weather_network")]))
    missing = sorted(path for path in paths if not Path(path).is_file())
    if missing:
        raise FileNotFoundError("Required external inputs are missing (build donors separately):\n" + "\n".join(missing))


def task_targets(task):
    targets = [task["target"]]
    settings = task["settings"]
    directory = f"{settings['output_directory']}/{task['case']}"
    targets += [f"{directory}/{report}.csv" for report in
                ("fuel_shedding_by_demand", "fuel_shedding_by_fuel", "fuel_shedding_timeseries",
                 "fuel_generator_disruptions", "fuel_link_disruptions",
                 "hydrogen_shedding_by_bus", "hydrogen_shedding_timeseries")]
    plotting = settings.get("plotting", {})
    if plotting.get("enable", True):
        label = plotting.get("region_label", "DK2")
        directory = f"{settings['output_directory']}/{task['case']}/plots"
        # Same paths as case_plot_file() in rules/plot_disruption.smk:
        # plots/<plot>/<plot>_<label>_<case>_<year>_<kind>.png
        year = settings.get("selected_weather_year")
        tag = f"{task['case']}_{year}" if year else task["case"]
        targets += [f"{directory}/{plot}/{plot}_{label}_{tag}_{kind}.png"
                    for plot in ("dispatch_year", "dispatch_zoom", "demand_shares", "production_shares")
                    for kind in ("power", "heat")]
    # Comparison plots are deliberately a separate, post-array invocation.
    return targets


def main(argv=None, default_configfiles=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--configfiles", nargs="+", default=default_configfiles or CONFIGFILES)
    parser.add_argument("--years", default=",".join(NEW_YEARS), help="Comma-separated July-June years")
    parser.add_argument("--cases", help="Comma-separated case names; default: all configured cases")
    parser.add_argument("--list", action="store_true", help="Print stable zero-based task mapping")
    parser.add_argument("--check-inputs", action="store_true", help="Check inputs for every selected task, then exit")
    parser.add_argument("--task-index", type=int, default=None)
    parser.add_argument("--run-year", action="store_true",
                        help="Run all selected cases sequentially for exactly one weather year; stop on failure")
    parser.add_argument("--cores", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", "1")))
    parser.add_argument("--dry-run", action="store_true")
    args, extra = parser.parse_known_args(argv)
    config = load_config(args.configfiles)
    tasks = campaign_tasks(config, args.years.split(","),
                           args.cases.split(",") if args.cases else None)
    if args.list:
        for index, task in enumerate(tasks):
            print(f"{index}\t{task['year']}\t{task['case']}\t{task['target']}")
        return
    if args.check_inputs:
        check_inputs(tasks)
        print(f"External inputs exist for all {len(tasks)} tasks.")
        return
    if args.run_year:
        if len(args.years.split(",")) != 1 or args.task_index is not None or "SLURM_ARRAY_TASK_ID" in os.environ:
            parser.error("--run-year requires exactly one year and no task index/Slurm array.")
        check_inputs(tasks)
        for index in range(len(tasks)):
            command = [sys.executable, "-m", "scripts.run_disruption_campaign",
                       "--configfiles", *args.configfiles, "--years", args.years,
                       "--task-index", str(index), "--cores", str(args.cores)]
            if args.cases:
                command += ["--cases", args.cases]
            if args.dry_run:
                command.append("--dry-run")
            subprocess.run(command + extra, check=True)
        return
    index = args.task_index
    if index is None and "SLURM_ARRAY_TASK_ID" in os.environ:
        index = int(os.environ["SLURM_ARRAY_TASK_ID"])
    if index is None or not 0 <= index < len(tasks):
        parser.error(f"Provide --task-index or SLURM_ARRAY_TASK_ID in 0..{len(tasks)-1}.")
    task = tasks[index]
    check_inputs([task])
    # Array tasks own disjoint case directories. No task builds shared inputs or
    # writes comparison plots; this is the only supported use of --nolock here.
    command = ["snakemake", *task_targets(task), "--executor", "local", "--nolock",
               "--allowed-rules", *RULES, "--rerun-triggers", "mtime",
               "--configfiles", *args.configfiles, "--cores", str(args.cores),
               "--resources", "mem_mb=64000", "--printshellcmds",
               "--config", *[f"{key}={value}" for key, value in task["selection"].items()]]
    if os.environ.get("TMPDIR"):
        command += ["--default-resources", f"tmpdir={os.environ['TMPDIR']}"]
    if args.dry_run:
        command.append("--dry-run")
    command += extra
    print(f"Task {index}: {task['year']} / {task['case']}", flush=True)
    print(shlex.join(command), flush=True)
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
