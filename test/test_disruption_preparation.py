"""Checks for fixed-capacity weather transfer and usable heat shedding."""
import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.prepare_network_disruptions import (
    add_load_shedding, add_fuel_backup_links, assert_fixed_capacities, set_weather,
)


def network(year):
    n = pypsa.Network()
    n.set_snapshots(pd.date_range(f"{year}-07-01", periods=2, freq="h"))
    n.add("Bus", "electricity", carrier="AC")
    n.add("Bus", "heat", carrier="rural heat")
    n.add("Load", "elec demand", bus="electricity", p_set=[4, 8])
    n.add("Load", "heat demand", bus="heat", p_set=[3, 5])
    n.add("Generator", "wind", bus="electricity", carrier="onwind",
          p_nom=10, p_max_pu=[0.2, 0.4])
    n.add("Generator", "nuclear", bus="electricity", carrier="nuclear",
          p_nom=5, p_max_pu=0.9)
    n.add("Link", "heat pump", bus0="electricity", bus1="heat",
          p_nom=2, efficiency=[2, 3])
    return n


def test_weather_matches_names_and_preserves_capacity_and_static_nuclear():
    base, donor = network(2013), network(1965)
    donor.loads = donor.loads.iloc[::-1]
    donor.loads_t.p_set = donor.loads_t.p_set.iloc[:, ::-1]
    donor.loads_t.p_set.loc[:, "elec demand"] = [6, 9]
    donor.generators_t.p_max_pu.loc[:, "wind"] = [0.8, 0.1]
    donor.generators.at["wind", "p_nom"] = 999
    donor.generators.at["nuclear", "p_max_pu"] = 0.1
    donor.links_t.efficiency.loc[:, "heat pump"] = [4, 5]
    snapshots = base.snapshots.copy()
    set_weather(base, donor)
    assert base.snapshots.equals(snapshots)
    assert base.generators.at["wind", "p_nom"] == 10
    assert base.generators.at["nuclear", "p_max_pu"] == 0.9
    assert base.loads_t.p_set["elec demand"].tolist() == [6, 9]
    assert base.generators_t.p_max_pu.wind.tolist() == [0.8, 0.1]
    assert base.links_t.efficiency["heat pump"].tolist() == [4, 5]


def test_calendar_mismatch_is_rejected():
    base, donor = network(2013), network(1965)
    donor.set_snapshots(donor.snapshots + pd.Timedelta(days=1))
    with pytest.raises(ValueError, match="calendar"):
        set_weather(base, donor)


def test_missing_load_is_rejected():
    base, donor = network(2013), network(1965)
    donor.remove("Load", "heat demand")
    with pytest.raises(ValueError, match="lacks Load"):
        set_weather(base, donor)


def test_shedding_refreshes_and_is_capped_by_hourly_demand():
    n = network(2013)
    add_load_shedding(n)
    n.loads_t.p_set.loc[:, "elec demand"] = [2, 12]
    add_load_shedding(n)
    name = "electricity load shedding electricity"
    assert n.generators.at[name, "p_nom"] == 12
    assert len(n.generators.index[n.generators.carrier.str.startswith("load-shedding")]) == 2
    np.testing.assert_allclose(n.generators_t.p_max_pu[name], [2 / 12, 1])
    assert_fixed_capacities(n)


def test_household_group_shedding_can_meet_assigned_demand():
    from scripts.household_heat_constraints import add_household_heat_constraints
    n = network(2013)
    n.remove("Generator", n.generators.index)
    n.remove("Link", "heat pump")
    n.add("Bus", "group heat", carrier="rural heat")
    n.add("Link", "delivery", bus0="group heat", bus1="heat", p_nom=1)
    n.meta = {"household_heat_allocation": {
        "mode": "dispatch", "shares": {"group": 1.0}, "groups": [{
            "group": "group", "heat_bus": "heat", "delivery": "delivery",
            "demand_loads": ["heat demand"], "additional_demand_loads": [],
        }],
    }}
    add_load_shedding(n)
    assert n.links.at["delivery", "p_nom"] == 5
    assert "group heat load shedding heat" in n.generators.index
    assert "heat load shedding heat" not in n.generators.index
    status, condition = n.optimize(
        solver_name="highs", extra_functionality=add_household_heat_constraints,
    )
    assert status == "ok" and condition == "optimal"
    np.testing.assert_allclose(n.generators_t.p["group heat load shedding heat"], [3, 5])


def test_dispatch_uses_existing_eur_solver_without_investment():
    from pathlib import Path
    from types import SimpleNamespace
    import yaml
    from scripts import solve_network as solver

    config = yaml.safe_load((Path(__file__).parents[1] / "config/config.default.yaml").read_text())
    config["solving"]["solver"] = {"name": "highs", "options": ""}
    n = network(2013)
    add_load_shedding(n)
    model_kwargs, solve_kwargs = solver.collect_kwargs(
        config, config["solving"], [2025], mode="single",
    )
    solver.create_optimization_model(
        n, config, SimpleNamespace(custom_extra_functionality=None),
        model_kwargs, solve_kwargs, planning_horizons="2025",
    )
    status, condition = n.optimize.solve_model(**solve_kwargs)
    assert status == "ok" and condition == "optimal"
    assert_fixed_capacities(n)
    assert n.generators.at["wind", "p_nom"] == 10


def test_infeasibility_report_maps_labels_and_preserves_diagnostic_errors(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts.solve_disruption_dispatch import write_infeasibility_report

    constraints = SimpleNamespace(print_labels=lambda labels: print(f"Bus balance conflict: {labels}"))
    model = SimpleNamespace(
        solver_model=Mock(), constraints=constraints,
        compute_infeasibilities=lambda: [4, 9],
    )
    report = tmp_path / "infeasibility.txt"
    write_infeasibility_report(SimpleNamespace(model=model), report, time_limit=10)
    assert "Bus balance conflict: [4, 9]" in report.read_text()
    model.solver_model.setParam.assert_called_once_with("TimeLimit", 10)
    model.solver_model.write.assert_called_once_with(str(report.with_suffix(".ilp")))

    model.compute_infeasibilities = Mock(side_effect=RuntimeError("IIS unavailable"))
    write_infeasibility_report(SimpleNamespace(model=model), report)
    assert "IIS unavailable" in report.read_text()


def test_parallel_fuel_backup_preserves_emissions_and_reports_weighted_dispatch(tmp_path):
    from scripts.solve_disruption_dispatch import save_fuel_backup_dispatch
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2013-07-01", periods=2, freq="h"))
    n.snapshot_weightings.loc[:, :] = np.array([[1], [2]])
    n.add("Bus", "oil", carrier="oil")
    n.add("Bus", "transport", carrier="land transport oil", country="NO")
    n.add("Bus", "co2", carrier="co2")
    n.add("Store", "atmosphere", bus="co2", e_nom=100)
    n.add("Generator", "oil source", bus="oil", p_nom=20, marginal_cost=10)
    n.add("Load", "travel", bus="transport", p_set=[4, 6])
    n.add("Link", "oil delivery", carrier="land transport oil", bus0="oil",
          bus1="transport", bus2="co2", efficiency2=[0.2, 0.3],
          p_nom=5, marginal_cost=[0.2, 0.3])
    # A physical link must not be backed up merely because it consumes fuel.
    n.add("Link", "boiler", carrier="oil boiler", bus0="oil", bus1="transport", p_nom=0)
    options = {"enable": True, "capacity_multiplier": 1.0, "marginal_cost_premium": 0.1}
    add_fuel_backup_links(n, options)
    add_fuel_backup_links(n, options)  # Refresh without adding duplicates.
    backup = "oil delivery fuel backup"
    assert len(n.meta["fuel_backup_links"]) == 1
    assert n.links.at[backup, "p_nom"] == 5
    assert n.links.at["oil delivery", "p_nom"] == 5
    assert n.links.at[backup, "bus2"] == "co2"
    np.testing.assert_allclose(n.links_t.efficiency2[backup], [0.2, 0.3])
    np.testing.assert_allclose(n.links_t.marginal_cost[backup], [0.3, 0.4])
    assert_fixed_capacities(n)
    status, condition = n.optimize(solver_name="highs")
    assert status == "ok" and condition == "optimal"
    np.testing.assert_allclose(n.links_t.p0[backup], [0, 1], atol=1e-7)
    np.testing.assert_allclose(n.links_t.p2[backup], [0, -0.3], atol=1e-7)
    path = tmp_path / "fuel_backup_dispatch.csv"
    save_fuel_backup_dispatch(n, path)
    row = pd.read_csv(path).iloc[0]
    assert (row.fuel, row.end_use, row.country) == ("oil", "land transport", "NO")
    assert row.peak_input_MW == pytest.approx(1)
    assert row.input_MWh == pytest.approx(2)
    assert row.delivered_MWh == pytest.approx(2)


def test_fuel_backup_can_be_disabled_and_empty_report_has_headers(tmp_path):
    from scripts.solve_disruption_dispatch import save_fuel_backup_dispatch
    n = network(2013)
    add_fuel_backup_links(n, {"enable": False})
    path = tmp_path / "fuel_backup_dispatch.csv"
    save_fuel_backup_dispatch(n, path)
    assert pd.read_csv(path).empty
    with pytest.raises(ValueError, match="positive"):
        add_fuel_backup_links(n, {"enable": True, "marginal_cost_premium": -1})
