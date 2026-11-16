"""Heat disposal follows installed output capacity, not demand or past vent use."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.heat_vent_capacity import (
    META_KEY,
    add_heat_vent_capacity_constraints,
    prepare_heat_vent_capacity,
)


def network():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2013-07-01", periods=4, freq="h"))
    for bus, carrier in (("grid", "AC"), ("fuel", "gas"), ("heat", "urban central heat"),
                         ("tank", "urban central water tanks")):
        n.add("Bus", bus, carrier=carrier)
    n.add("Generator", "heat vent", bus="heat", carrier="urban central heat vent",
          p_nom=0.007, p_nom_opt=0.007, p_min_pu=-1, p_max_pu=0, marginal_cost=-0.02)
    return n


def test_sum_thermal_outputs_reverse_heat_pump_and_storage_discharge():
    n = network()
    # CHP contributes heat output, not fuel input or its electrical output.
    n.add("Link", "CHP", bus0="fuel", bus1="grid", bus2="heat",
          p_nom=100, efficiency=0.4, efficiency2=[0.4, 0.5, 0.6, 0.5])
    n.add("Link", "boiler", bus0="fuel", bus1="heat", p_nom=20, efficiency=0.9)
    # The repository's reverse-operated pumps are rated at the heat-output bus0.
    n.add("Link", "pump", bus0="heat", bus1="grid", p_nom=30,
          p_min_pu=-1, p_max_pu=0, efficiency=[1/3, 1/2, 1/3, 1/2])
    n.add("Generator", "solar thermal", bus="heat", p_nom=10, p_max_pu=[0, 0.5, 1, 0.5])
    n.add("Link", "tank discharge", bus0="tank", bus1="heat", p_nom=10, efficiency=0.8)
    n.add("Link", "tank charge", bus0="heat", bus1="tank", p_nom=500, efficiency=0.9)
    n.add("Generator", "heat shortage", bus="heat", carrier="load-shedding-heat", p_nom=10000)
    n.add("Load", "heat demand", bus="heat", p_set=1e6)
    capacities = n.links.p_nom.copy()
    prepare_heat_vent_capacity(n)
    # 60 CHP + 18 boiler + 30 heat pump + 10 solar + 8 discharge = 126 MW_th.
    vent = n.generators.loc["heat vent"]
    assert vent.p_nom == pytest.approx(126)
    assert vent.p_nom_opt == pytest.approx(126)
    assert not vent.p_nom_extendable
    assert (vent.p_min_pu, vent.p_max_pu, vent.sign) == (-1, 0, 1)
    assert vent.marginal_cost == -0.02
    pd.testing.assert_series_equal(n.links.p_nom, capacities)
    # Repeating preparation is safe; neither previous vent size nor shedding counts.
    prepare_heat_vent_capacity(n)
    assert n.generators.at["heat vent", "p_nom"] == pytest.approx(126)
    assert len(n.meta[META_KEY]["vents"]["heat vent"]["sources"]) == 5


def test_adding_demand_or_shedding_cannot_increase_disposal_capacity():
    n = network()
    n.add("Link", "boiler", bus0="fuel", bus1="heat", p_nom=10, efficiency=0.9)
    prepare_heat_vent_capacity(n)
    first = n.generators.at["heat vent", "p_nom"]
    n.add("Load", "demand", bus="heat", p_set=100000)
    n.add("Generator", "shedding", bus="heat", carrier="load-shedding-heat", p_nom=100000)
    prepare_heat_vent_capacity(n)
    assert n.generators.at["heat vent", "p_nom"] == first == 9


def test_no_vents_stays_disabled_and_nonfrozen_source_fails_without_changes():
    n = network()
    n.add("Link", "boiler", bus0="fuel", bus1="heat", p_nom_extendable=True)
    with pytest.raises(ValueError, match="Freeze"):
        prepare_heat_vent_capacity(n)
    assert n.generators.at["heat vent", "p_nom"] == 0.007
    n.remove("Generator", "heat vent")
    prepare_heat_vent_capacity(n)
    assert n.generators.empty


def test_multiple_ports_at_same_heat_bus_use_net_injection():
    n = network()
    n.add("Link", "heat transfer", bus0="heat", bus1="heat", p_nom=10,
          p_min_pu=-1, p_max_pu=0, efficiency=0.8)
    prepare_heat_vent_capacity(n)
    assert n.generators.at["heat vent", "p_nom"] == pytest.approx(2)


def test_nonfinite_heat_output_profile_is_rejected():
    n = network()
    n.add("Link", "boiler", bus0="fuel", bus1="heat", p_nom=10,
          efficiency=[0.9, np.nan, 0.9, 0.9])
    with pytest.raises(ValueError, match="Non-finite"):
        prepare_heat_vent_capacity(n)
    assert n.generators.at["heat vent", "p_nom"] == 0.007


@pytest.mark.parametrize("mode", ["investment", "dispatch", "rolling"])
def test_chp_can_supply_electricity_with_zero_heat_demand(mode, tmp_path):
    n = network()
    n.add("Generator", "fuel supply", bus="fuel", p_nom=100, marginal_cost=1)
    n.add("Generator", "shortage", bus="grid", carrier="load-shedding-electricity",
          p_nom=10, marginal_cost=10000)
    n.add("Link", "CHP", bus0="fuel", bus1="grid", bus2="heat",
          p_nom=0 if mode == "investment" else 10,
          p_nom_extendable=mode == "investment", capital_cost=10,
          efficiency=0.5, efficiency2=0.5)
    n.add("Load", "electricity demand", bus="grid", p_set=5)
    prepare_heat_vent_capacity(n, investment=mode == "investment")
    if mode == "rolling":
        # A solved reference is needed for initial states; no production model is run.
        reference = n.copy()
        assert reference.optimize(solver_name="highs", log_to_console=False) == ("ok", "optimal")
        reference.model.solver_model = None
        from scripts.rolling_disruption_dispatch import solve_rolling_dispatch
        from scripts import solve_network as solver
        config = yaml.safe_load((Path(__file__).parents[1] / "config/config.default.yaml").read_text())
        config["solving"]["solver"] = {"name": "highs", "options": ""}
        model, solve = solver.collect_kwargs(config, config["solving"], [2025], mode="single")
        solve["log_to_console"] = False
        solve_rolling_dispatch(n, reference, config, SimpleNamespace(custom_extra_functionality=None),
                              model, solve, 2025, {"horizon": 3, "overlap": 1,
                              "storage": {"msv": {"enable": False}}})
    else:
        # Exercise the production model hook for capacity-expansion/base solves.
        from scripts import solve_network as solver
        config = yaml.safe_load((Path(__file__).parents[1] / "config/config.default.yaml").read_text())
        config["solving"]["solver"] = {"name": "highs", "options": ""}
        model, solve = solver.collect_kwargs(config, config["solving"], [2025], mode="single")
        solve["log_to_console"] = False
        solver.create_optimization_model(n, config, SimpleNamespace(custom_extra_functionality=None), model, solve, "2025")
        assert n.optimize.solve_model(**solve) == ("ok", "optimal")
    np.testing.assert_allclose(n.generators_t.p["shortage"], 0, atol=1e-7)
    np.testing.assert_allclose(n.links_t.p1["CHP"], -5, atol=1e-7)
    np.testing.assert_allclose(n.generators_t.p["heat vent"], -5, atol=1e-7)
    assert n.generators.at["heat vent", "p_nom_opt"] == pytest.approx(5)
    # The saved base carries the installed output-sized sink into fixed dispatch.
    n.model.solver_model = None
    n.export_to_netcdf(tmp_path / f"{mode}.nc")
    frozen = pypsa.Network(tmp_path / f"{mode}.nc")
    frozen.optimize.fix_optimal_capacities()
    prepare_heat_vent_capacity(frozen)
    assert frozen.generators.at["heat vent", "p_nom"] == pytest.approx(5)
    assert not frozen.generators.at["heat vent", "p_nom_extendable"]


def test_larger_baseline_heat_demand_does_not_set_vent_rating():
    n = network()
    n.add("Generator", "heat plant", bus="heat", p_nom=100, marginal_cost=1)
    n.add("Load", "demand", bus="heat", p_set=99.9)
    prepare_heat_vent_capacity(n, investment=True)
    assert n.optimize(solver_name="highs", log_to_console=False,
                      extra_functionality=lambda n, snapshots: add_heat_vent_capacity_constraints(n)) == ("ok", "optimal")
    assert n.generators.at["heat vent", "p_nom_opt"] == pytest.approx(100)
    np.testing.assert_allclose(n.generators_t.p["heat vent"], 0, atol=1e-7)
