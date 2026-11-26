"""One live status record; detailed state is written only on failure."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import traceback

import pandas as pd


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def close_solver_log(n, solver_name):
    """Close the native log stream before rotating the next window's log."""
    solver = getattr(getattr(n, "model", None), "solver_model", None)
    if solver is None:
        return
    try:
        if solver_name == "gurobi":
            solver.setParam("LogFile", "")
        elif solver_name == "highs":
            solver.setOptionValue("log_file", "")
    except Exception:
        logging.getLogger(__name__).warning("Could not close the solver log stream", exc_info=True)


class RollingDiagnostics:
    def __init__(self, output_directory, config):
        self.directory = Path(output_directory) if output_directory is not None else None
        self.current = {}
        self._failure_directory = None
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            write_json(self.directory / "run_info.json", config)
            (self.directory / "window_summary.csv").unlink(missing_ok=True)
        self.phase("initializing")

    @property
    def failure_directory(self):
        if self.directory is None:
            return None
        if self._failure_directory is None:
            self._failure_directory = self.directory / "failures" / self.run_id
            self._failure_directory.mkdir(parents=True, exist_ok=False)
        return self._failure_directory

    def phase(self, phase, **details):
        self.current.update(phase=phase, run_id=self.run_id,
                            updated_utc=datetime.now(timezone.utc).isoformat(), **details)
        if self.directory is not None:
            write_json(self.directory / "status.json", self.current)

    @staticmethod
    def opening_states(n):
        rows = []
        for component, initial in (("Store", "e_initial"), ("StorageUnit", "state_of_charge_initial")):
            for name, row in n.components[component].static.iterrows():
                rows.append({"component": component, "asset": name, "carrier": row.carrier,
                             "bus": row.bus, "opening_MWh": float(row[initial])})
        return rows

    def failure(self, exc, n=None, budgets=(), opening=None, resources=None):
        current_solver_log = "window" in self.current or self.current.get("phase") in ("solving", "solve_failed")
        self.phase("failed", exception_type=type(exc).__name__, error=str(exc))
        destination = self.failure_directory
        if destination is None:
            return
        context = json.dumps(self.current, indent=2, default=str)
        (destination / "failure.txt").write_text(context + "\n\n" + traceback.format_exc(), encoding="utf-8")
        if n is not None:
            rows = opening if opening is not None else self.opening_states(n)
            pd.DataFrame(rows, columns=["component", "asset", "carrier", "bus", "opening_MWh"]).to_csv(
                destination / "storage_opening.csv", index=False)
            rows = resources if resources is not None else [asdict(b) for b in budgets]
            frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["identifier", "carrier", "remaining_MWh"])
            frame.to_csv(destination / "resource_remaining.csv", index=False)
        log = self.directory / "solver.log"
        if current_solver_log and log.exists():
            shutil.copy2(log, destination / "solver.log")

    @contextmanager
    def window(self, n, number, window, implemented, budgets):
        self.phase("building_model", window=number,
                   optimization_start=str(window[0]), optimization_end=str(window[-1]),
                   implemented_start=str(implemented[0]), implemented_end=str(implemented[-1]))
        if self.directory is not None:
            (self.directory / "solver.log").unlink(missing_ok=True)
        opening = self.opening_states(n) if self.directory is not None else None
        resources = [asdict(b) for b in budgets] if self.directory is not None else None
        try:
            yield
        except Exception as exc:
            self.failure(exc, n, budgets, opening, resources)
            raise

    def committed(self, row):
        if self.directory is not None:
            path = self.directory / "window_summary.csv"
            pd.DataFrame([row]).to_csv(path, mode="a", header=not path.exists(), index=False)
        self.phase("window_completed", last_completed_window=row["window"])
