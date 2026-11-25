"""Small persistent rolling-window records, independent of workflow outputs."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import traceback

import pandas as pd


class RollingDiagnostics:
    def __init__(self, output_directory, config):
        self.directory = None
        self.current = {}
        if output_directory is None:
            return
        root = Path(output_directory)
        attempt = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
        self.directory = root / "diagnostics" / attempt
        self.directory.mkdir(parents=True, exist_ok=False)
        self.pointer = root / "latest_diagnostics.json"
        self.write_json("config.json", config)
        code_root = Path(__file__).resolve().parent
        names = ["rolling_disruption_dispatch.py", "solve_disruption_dispatch.py",
                 "disruption_resource_budgets.py", "disruption_storage_msv.py",
                 "disruption_waste_hvc.py", "prepare_network_disruptions.py",
                 "interconnector_disruption.py", "household_heat_constraints.py",
                 "solve_network.py", "disruption_diagnostics.py", "fuel_demand_shedding.py",
                 "fuel_generator_disruption.py", "fuel_link_disruption.py",
                 "hydrogen_balance_shedding.py"]
        self.write_json("provenance.json", {
            "slurm": {key: os.environ.get(key) for key in
                      ("SLURM_JOB_ID", "SLURM_ARRAY_JOB_ID", "SLURM_ARRAY_TASK_ID", "SLURM_JOB_NAME")},
            "script_sha256": {name: hashlib.sha256((code_root / name).read_bytes()).hexdigest()
                              for name in names},
        })
        self.phase("initializing")

    def write_json(self, name, value):
        (self.directory / name).write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")

    def phase(self, phase, **details):
        if self.directory is None:
            return
        self.current.update(phase=phase, updated_utc=datetime.now(timezone.utc).isoformat(), **details)
        self.write_json("window_context.json", self.current)
        self.pointer.write_text(json.dumps({
            "directory": str(Path("diagnostics") / self.directory.name),
            **self.current,
        }, indent=2, default=str), encoding="utf-8")

    def append(self, name, rows):
        if not rows:
            return
        path = self.directory / name
        pd.DataFrame(rows).to_csv(path, mode="a", header=not path.exists(), index=False)

    @contextmanager
    def window(self, n, number, window, implemented, budgets):
        if self.directory is not None:
            self.phase("building_model", window=number,
                       optimization_start=str(window[0]), optimization_end=str(window[-1]),
                       implemented_start=str(implemented[0]), implemented_end=str(implemented[-1]))
            self.append("resource_ledger.csv", [{"window": number, **asdict(b)} for b in budgets])
            rows = []
            for component, initial in (("Store", "e_initial"), ("StorageUnit", "state_of_charge_initial")):
                table = n.components[component].static
                for name, row in table.iterrows():
                    rows.append({"window": number, "component": component, "asset": name,
                                 "carrier": row.carrier, "bus": row.bus,
                                 "opening_MWh": float(row[initial])})
            self.append("storage_opening.csv", rows)
        try:
            yield
        except Exception as exc:
            self.phase("failed", exception_type=type(exc).__name__, error=str(exc),
                       traceback=traceback.format_exc())
            raise

    def committed(self, row):
        if self.directory is not None:
            self.append("rolling_progress.csv", [row])
            self.phase("window_completed", window=row["window"])
