"""List/check/submit independent baseline or disruption tasks on Sophia."""
import argparse
from copy import deepcopy
import csv
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml

from scripts.disruption_scenarios import select_disruption, common_policy
from scripts.analyse_disruption_windows import TIMING_METRICS, select_campaign_windows

YEARS = ["2013-2014", "1965-1966", "1968-1969", "1988-1989", "2019-2020"]
VARIANTS = ["2025", "2035_wo_V2G", "2035_w_V2G"]


def merge_config(target, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_config(target[key], value)
        else:
            target[key] = deepcopy(value)


def load_config(paths):
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config/config.default.yaml").read_text(encoding="utf-8"))
    for path in paths:
        merge_config(config, yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {})
    return config


def configfiles(variant, campaign, sophia=False):
    paths = [f"config/config.sector_{variant}.yaml"]
    if sophia:
        paths.append("config/config.sophia.yaml")
    paths.append("config/config.disruption_common.yaml")
    if campaign != "baseline":
        paths.append(f"config/config.{'fuel' if campaign == 'fuel' else 'interconnector'}_disruption.yaml")
    return paths


def campaign_tasks(config, years, cases=None, variant=None):
    tasks = []
    for year in years:
        selection = {"disruption_weather_year": year}
        if variant:
            selection["disruption_variant"] = variant
        settings = select_disruption({**config, **selection})
        for case in cases or settings["cases"]:
            if case not in settings["cases"]:
                raise ValueError(f"Unknown case {case}.")
            tasks.append({"year": year, "variant": settings["selected_variant"], "case": case,
                          "selection": {**selection, "disruption_case": case}, "settings": settings,
                          "definition": settings["cases"][case],
                          "target": f"{settings['output_directory']}/{case}/network_solved.nc"})
    targets = [task["target"] for task in tasks]
    if not tasks or len(set(targets)) != len(targets):
        raise ValueError("Campaign must have tasks with unique output paths.")
    return tasks


def check_inputs(tasks):
    paths = set()
    for task in tasks:
        s = task["settings"]
        paths.update(filter(None, [s["base_network"], s.get("household_shares"), s.get("weather_network")]))
        if s["campaign"] != "baseline":
            paths.update(f"{s['baseline_directory']}/{name}" for name in
                         ("network_prepared.nc", "base_weather/network_solved.nc", "event_windows.csv",
                          "base_weather/run_info.json", "base_weather/status.json"))
    missing = sorted(path for path in paths if not Path(path).is_file())
    if missing:
        raise FileNotFoundError("Missing inputs; build references/donors, then run baselines before disruptions:\n" + "\n".join(missing))
    checked = set()
    for task in tasks:
        s = task["settings"]
        if s["campaign"] == "baseline" or s["baseline_directory"] in checked:
            continue
        checked.add(s["baseline_directory"])
        directory = Path(s["baseline_directory"]) / "base_weather"
        info = json.loads((directory / "run_info.json").read_text())
        status = json.loads((directory / "status.json").read_text())
        effective = load_config(task.get("configfiles") or configfiles(task["variant"], s["campaign"]))
        solving = effective["solving"]
        if status.get("phase") != "complete" or info.get("common_policy") != common_policy(s, solving, effective):
            raise ValueError(f"Baseline incomplete or common settings changed: {directory}. Rerun baselines first.")
        sources = [Path(value) for value in (s["base_network"], s.get("household_shares"), s.get("weather_network")) if value]
        sources += [Path(__file__).parent / name for name in (
            "prepare_network_disruptions.py", "solve_disruption_dispatch.py", "rolling_disruption_dispatch.py",
            "heat_vent_capacity.py", "household_heat_constraints.py", "disruption_storage_msv.py",
            "disruption_resource_budgets.py", "fuel_demand_shedding.py", "hydrogen_balance_shedding.py",
            "disruption_waste_hvc.py", "disruption_diagnostics.py", "disruption_scenarios.py",
            "resource_budget_duals.py", "solve_network.py")]
        solved_time = (directory / "network_solved.nc").stat().st_mtime_ns
        if any(path.stat().st_mtime_ns > solved_time for path in sources):
            raise ValueError(f"Baseline source inputs/code changed: {directory}. Rerun baselines first.")
        timing = directory.parent / "event_windows.csv"
        analysis_code = Path(__file__).parent / "analyse_disruption_windows.py"
        if timing.stat().st_mtime_ns < max(solved_time, analysis_code.stat().st_mtime_ns):
            raise ValueError(f"Event windows are stale: {timing}. Rerun baseline timing analysis first.")
        with timing.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        select_campaign_windows(rows, s["campaign"], s["durations_hours"])
        if any(row["variant"] != task["variant"] or row["weather_year"] != task["year"] for row in rows):
            raise ValueError(f"Event window identity differs from campaign: {timing}.")




def task_targets(task):
    directory = str(Path(task["target"]).parent).replace("\\", "/")
    targets = [f"{directory}/{name}" for name in
               ("network_solved.nc", "dispatch_summary.csv", "window_summary.csv", "resource_usage.csv")]
    if task["settings"]["campaign"] == "baseline":
        targets.append(f"{task['settings']['baseline_directory']}/event_windows.csv")
    else:
        targets.append(f"{directory}/applied_disruption.csv")
    return targets


def plan_rows(tasks):
    for index, task in enumerate(tasks):
        start = end = ""
        hours = task["definition"].get("duration_hours", "")
        timing = Path(task["settings"]["baseline_directory"]) / "event_windows.csv"
        if hours and timing.exists():
            with timing.open(newline="", encoding="utf-8-sig") as stream:
                selected = select_campaign_windows(list(csv.DictReader(stream)), task["settings"]["campaign"])
                matches = selected.loc[selected.duration_hours.eq(hours)].to_dict("records")
            if len(matches) != 1:
                raise ValueError(f"Missing or duplicate duration {hours} in {timing}.")
            start, end = matches[0]["start"], matches[0]["end"]
        yield {"task_index": index, "campaign": task["settings"]["campaign"],
               "timing_metric": TIMING_METRICS.get(task["settings"]["campaign"], ""),
               "variant": task["variant"], "weather_year": task["year"],
               "case": task["case"], "duration_hours": hours, "start": start, "end": end,
               "baseline": f"{task['settings']['baseline_directory']}/base_weather/network_solved.nc",
               "target": task["target"]}


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--campaign", choices=["baseline", "interconnector", "fuel"], required=True)
    parser.add_argument("--variant", choices=[*VARIANTS, "all"], default="2025")
    parser.add_argument("--years", default=",".join(YEARS))
    parser.add_argument("--cases", help="Comma-separated generated case names")
    parser.add_argument("--sophia", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--write-plan", type=Path, help="Write a reviewable CSV task mapping; does not run jobs")
    parser.add_argument("--check-inputs", action="store_true")
    parser.add_argument("--task-index", type=int)
    parser.add_argument("--cores", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", "1")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    tasks = []
    for variant in VARIANTS if args.variant == "all" else [args.variant]:
        paths = configfiles(variant, args.campaign, args.sophia)
        config = load_config(paths)
        for task in campaign_tasks(config, args.years.split(","), args.cases.split(",") if args.cases else None, variant):
            task["configfiles"] = paths
            tasks.append(task)
    if args.check_inputs or args.write_plan:
        check_inputs(tasks)
    if args.list or args.write_plan:
        rows = list(plan_rows(tasks))
        if args.write_plan:
            args.write_plan.parent.mkdir(parents=True, exist_ok=True)
            with args.write_plan.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        if args.list:
            for row in rows:
                print("\t".join(str(value) for value in row.values()))
        return
    if args.check_inputs:
        print(f"Inputs exist for all {len(tasks)} tasks.")
        return
    index = args.task_index
    if index is None and "SLURM_ARRAY_TASK_ID" in os.environ:
        index = int(os.environ["SLURM_ARRAY_TASK_ID"])
    if index is None or not 0 <= index < len(tasks):
        parser.error(f"Provide --task-index or SLURM_ARRAY_TASK_ID in 0..{len(tasks)-1}.")
    task = tasks[index]
    check_inputs([task])
    rules = (["prepare_disruption_network", "solve_disruption_baseline", "analyse_disruption_windows"]
             if args.campaign == "baseline" else ["apply_disruption", "solve_disruption_dispatch"])
    # Each task owns a different output directory. Disruption jobs cannot build shared inputs.
    command = ["snakemake", *task_targets(task), "--executor", "local", "--nolock", "--rerun-incomplete",
               "--allowed-rules", *rules, "--configfiles", *task["configfiles"],
               "--cores", str(args.cores), "--resources", "mem_mb=64000", "--printshellcmds",
               "--config", *[f"{key}={value}" for key, value in task["selection"].items()]]
    if os.environ.get("TMPDIR"):
        command += ["--default-resources", f"tmpdir={os.environ['TMPDIR']}"]
    if args.dry_run:
        command.append("--dry-run")
    print(f"Task {index}: {task['variant']} / {task['year']} / {task['case']}", flush=True)
    print(shlex.join(command), flush=True)
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
