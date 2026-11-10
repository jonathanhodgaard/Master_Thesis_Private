"""Checks for fixed-capacity weather transfer and usable heat shedding."""
import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.prepare_network_disruptions import (
    add_load_shedding, assert_fixed_capacities, set_weather,
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
