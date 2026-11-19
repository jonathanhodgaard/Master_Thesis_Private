from types import SimpleNamespace
import json

import pandas as pd
import pytest

from scripts.disruption_diagnostics import RollingDiagnostics
from scripts.disruption_resource_budgets import ResourceBudget


def test_storage_states_and_prior_attempt_survive_a_retry(tmp_path):
    stores = pd.DataFrame([{"carrier": "gas", "bus": "gas", "e_initial": 25.0}], index=["tank"])
    units = pd.DataFrame([{"carrier": "hydro", "bus": "AC", "state_of_charge_initial": 3.0}], index=["reservoir"])
    n = SimpleNamespace(components={"Store": SimpleNamespace(static=stores),
                                    "StorageUnit": SimpleNamespace(static=units)})
    window = pd.date_range("2013-07-01", periods=4, freq="h")
    budget = ResourceBudget("bio", "biomass", ("supply",), 100, 40, 5, True, True)
    first = RollingDiagnostics(tmp_path, {"test": "settings"})
    with pytest.raises(ValueError, match="test failure"):
        with first.window(n, 2, window, window[:2], [budget]):
            first.phase("solve_failed", solver_status="warning", termination_condition="infeasible")
            raise ValueError("test failure")
    storage = pd.read_csv(first.directory / "storage_opening.csv")
    assert storage.opening_MWh.tolist() == [25, 3]
    context = json.loads((first.directory / "window_context.json").read_text())
    assert context["window"] == 2
    assert context["termination_condition"] == "infeasible"
    second = RollingDiagnostics(tmp_path, {})
    assert first.directory != second.directory
    assert (first.directory / "storage_opening.csv").exists()
    pointer = json.loads((tmp_path / "latest_diagnostics.json").read_text())
    assert pointer["directory"].endswith(second.directory.name)


def test_diagnostics_can_be_disabled():
    diagnostics = RollingDiagnostics(None, {})
    diagnostics.phase("solving")
    with diagnostics.window(None, 0, None, None, []):
        pass
    diagnostics.committed({"window": 0})
    assert diagnostics.directory is None


def test_iis_keeps_full_model_and_more_than_first_hundred_equations(tmp_path):
    from scripts.solve_disruption_dispatch import write_infeasibility_report

    class Solver:
        IISMinimal = False

        def setParam(self, name, value):
            assert (name, value) == ("TimeLimit", 300)

        def write(self, path):
            from pathlib import Path
            Path(path).write_text("model capture")

    def print_labels(labels):
        for label in labels:
            print(f"constraint-{label}")

    model = SimpleNamespace(
        solver_model=Solver(),
        to_netcdf=lambda path: path.write_text("full precision capture"),
        compute_infeasibilities=lambda: list(range(150)),
        constraints=SimpleNamespace(print_labels=print_labels),
    )
    report = tmp_path / "infeasibility_window_0002.txt"
    write_infeasibility_report(SimpleNamespace(model=model), report)
    assert "constraint-99\n" in report.read_text()
    assert "constraint-149\n" not in report.read_text()
    assert "constraint-149\n" in report.with_suffix(".constraints.txt").read_text()
    for suffix in (".mps", ".model.nc", ".ilp"):
        assert report.with_suffix(suffix).is_file()
