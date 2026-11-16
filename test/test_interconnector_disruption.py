"""Scheduled outages: calendar validation, flow blocking and rolling overlap."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.interconnector_disruption import apply_interconnector_outages
from scripts.prepare_network_disruptions import set_weather


def network():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2014-01-01", periods=8, freq="h"))
    for carrier in ("AC", "DC"):
        n.add("Carrier", carrier)
    for bus in ("east", "west", "sweden"):
        n.add("Bus", bus, carrier="AC", v_nom=220)
    n.add("Link", "cable", bus0="west", bus1="east", carrier="DC", p_nom=10)
    n.add("Link", "cable-reversed", bus0="east", bus1="west", carrier="DC", p_nom=10)
    n.add("Line", "4", bus0="east", bus1="sweden", s_nom=10, x=0.1, r=0.01)
    n.add("Generator", "western supply", bus="west", p_nom=20, marginal_cost=10)
    n.add("Generator", "swedish supply", bus="sweden", p_nom=20, marginal_cost=11)
    n.add("Generator", "local backup", bus="east", p_nom=20, marginal_cost=100)
    n.add("Load", "demand", bus="east", p_set=5)
    return n


def event(n, start=3, end=6):
    return {
        "name": "isolation", "start": str(n.snapshots[start]),
        "end": str(n.snapshots[0] + pd.Timedelta(hours=end)),
        "lines": ["4"], "links": ["cable", "cable-reversed"],
    }


def test_existing_profiles_capacities_weather_and_export(tmp_path):
    n = network()
    donor = network()
    donor.links_t.p_max_pu["cable"] = np.linspace(0.5, 1, 8)
    donor.lines_t.s_max_pu["4"] = np.linspace(0.6, 0.9, 8)
    # Weather transfer first; the outage must preserve the transferred profiles.
    set_weather(n, donor)
    n.links.at["cable", "p_min_pu"] = -0.8
    original = {
        (c, a): n.get_switchable_as_dense(c, a).copy()
        for c, a in (("Link", "p_min_pu"), ("Link", "p_max_pu"), ("Line", "s_max_pu"))
    }
    capacities = n.links.p_nom.copy(), n.lines.s_nom.copy()
    apply_interconnector_outages(n, [event(n), event(n, 5, 8)])
    for (component, attr), expected in original.items():
        expected.iloc[3:] = 0
        pd.testing.assert_frame_equal(
            n.get_switchable_as_dense(component, attr), expected,
            check_column_type=False,  # Pandas may materialise string labels as object.
        )
    pd.testing.assert_series_equal(n.links.p_nom, capacities[0])
    pd.testing.assert_series_equal(n.lines.s_nom, capacities[1])
    n.export_to_netcdf(tmp_path / "outage.nc")
    loaded = pypsa.Network(tmp_path / "outage.nc")
    np.testing.assert_allclose(loaded.links_t.p_max_pu, n.links_t.p_max_pu)
    assert loaded.meta["interconnector_outages"][0]["hours"] == 3


@pytest.mark.parametrize("change,match", [
    ({"start": None}, "explicit"),
    ({"start": "2025-01-01"}, "precede"),
    ({"start": "2014-01-01 03:30"}, "calendar"),
    ({"end": "2014-01-02"}, "calendar"),
    ({"end": "2014-01-01 03:00"}, "precede"),
    ({"start": "2014-01-01 03:00Z"}, "timezone"),
    ({"lines": [4]}, "string"),
    ({"lines": ["unknown"]}, "Unknown Line"),
    ({"links": ["cable", "cable"]}, "Duplicate"),
    ({"lines": [], "links": []}, "at least one"),
    ({"strat": "2014-01-01"}, "Unknown outage"),
])
def test_invalid_schedule_does_not_partially_apply(change, match):
    n = network()
    original = n.get_switchable_as_dense("Link", "p_max_pu").copy()
    invalid = {**event(n), **change}
    with pytest.raises(ValueError, match=match):
        apply_interconnector_outages(n, [event(n), invalid])
    pd.testing.assert_frame_equal(n.get_switchable_as_dense("Link", "p_max_pu"), original)
    assert "interconnector_outages" not in n.meta


def test_empty_schedule_is_noop_and_meshed_lines_rejected():
    n = network()
    apply_interconnector_outages(n, [])
    assert n.links_t.p_max_pu.empty
    n.add("Line", "parallel", bus0="east", bus1="sweden", s_nom=10, x=0.1)
    with pytest.raises(ValueError, match="topology/KVL"):
        apply_interconnector_outages(n, [event(n)])


@pytest.mark.parametrize("rolling", [False, True])
@pytest.mark.parametrize("direction", ["import", "export"])
def test_actual_flows_stop_and_recover_across_rolling_boundaries(rolling, direction):
    n = network()
    if direction == "export":
        n.loads.at["demand", "bus"] = "west"
        n.generators.loc[["western supply", "swedish supply"], "marginal_cost"] = 100
        n.generators.at["local backup", "marginal_cost"] = 10
    assert n.optimize(solver_name="highs", log_to_console=False) == ("ok", "optimal")
    n.model.solver_model = None
    reference = n.copy()
    apply_interconnector_outages(n, [event(n)])
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/config.default.yaml").read_text())
    config["solving"]["solver"] = {"name": "highs", "options": ""}
    from scripts import solve_network as solver
    from scripts.rolling_disruption_dispatch import solve_rolling_dispatch
    model_kwargs, solve_kwargs = solver.collect_kwargs(config, config["solving"], [2025], mode="single")
    solve_kwargs["log_to_console"] = False
    params = SimpleNamespace(custom_extra_functionality=None)
    if rolling:
        reports = solve_rolling_dispatch(
            n, reference, config, params, model_kwargs, solve_kwargs, 2025,
            {"horizon": 4, "overlap": 2, "storage": {"msv": {"enable": False}}},
        )
        assert reports["window_summary"].implemented_hours.sum() == 8
    else:
        solver.create_optimization_model(n, deepcopy(config), params, model_kwargs, solve_kwargs, "2025")
        assert n.optimize.solve_model(**solve_kwargs) == ("ok", "optimal")
    outage = n.snapshots[3:6]
    available = n.snapshots.difference(outage)
    np.testing.assert_allclose(n.links_t.p0.loc[outage], 0, atol=1e-6)
    np.testing.assert_allclose(n.links_t.p1.loc[outage], 0, atol=1e-6)
    np.testing.assert_allclose(n.lines_t.p0.loc[outage], 0, atol=1e-6)
    np.testing.assert_allclose(n.lines_t.p1.loc[outage], 0, atol=1e-6)
    supplier = "local backup" if direction == "import" else "western supply"
    np.testing.assert_allclose(n.generators_t.p.loc[outage, supplier], 5, atol=1e-6)
    np.testing.assert_allclose(n.generators_t.p.loc[available, supplier], 0, atol=1e-6)
    assert n.snapshots.is_unique


def test_consolidated_configuration():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config/config.interconnector_disruption.yaml").read_text())
    settings = config["disruption"]
    rolling = settings["rolling_horizon"]
    assert rolling["horizon"] > rolling["overlap"] >= 0
    assert settings["cases"]["base_weather"]["interconnector_outages"] == []
    for case in settings["cases"].values():
        for outage in case.get("interconnector_outages", []):
            start, end = pd.Timestamp(outage["start"]), pd.Timestamp(outage["end"])
            assert start < end
            assert start == start.floor("h") and end == end.floor("h")
            assert outage.get("lines") or outage.get("links")
            assert all(isinstance(name, str) for name in outage.get("lines", []))
            for name in outage.get("links", []):
                counterpart = name.removesuffix("-reversed") if name.endswith("-reversed") else name + "-reversed"
                assert counterpart in outage["links"]


def test_distribution_and_heat_vent_adjustments_survive_outage_export(tmp_path):
    from scripts.prepare_network_disruptions import apply_low_voltage_capacity_multipliers
    from scripts.heat_vent_capacity import prepare_heat_vent_capacity
    n = network()
    for carrier in ("low voltage", "electricity distribution grid", "urban central heat", "urban central heat vent"):
        n.add("Carrier", carrier)
    for country in ("FI", "SE"):
        n.add("Bus", country+" grid", carrier="AC", country=country)
        n.add("Bus", country+" low voltage", carrier="low voltage", country=country)
        n.add("Bus", country+" heat", carrier="urban central heat", country=country)
        for suffix,source,destination in ((""," grid"," low voltage"),("-reversed"," low voltage"," grid")):
            n.add("Link", country+" distribution"+suffix,bus0=country+source,bus1=country+destination,
                  carrier="electricity distribution grid",p_nom=10,efficiency=.97)
        n.add("Link", country+" heater",bus0=country+" grid",bus1=country+" heat",p_nom=20,efficiency=.9)
        n.add("Generator", country+" vent",bus=country+" heat",carrier="urban central heat vent",
              p_nom=.001,p_min_pu=-1,p_max_pu=0,marginal_cost=-.02)
    apply_low_voltage_capacity_multipliers(n,{"FI":1.15,"SE":1.02})
    prepare_heat_vent_capacity(n)
    apply_interconnector_outages(n,[event(n)])
    path=tmp_path/"prepared_and_disrupted.nc"
    n.export_to_netcdf(path)
    loaded=pypsa.Network(path)
    for country,capacity in (("FI",11.5),("SE",10.2)):
        for suffix in ("","-reversed"):
            assert loaded.links.at[country+" distribution"+suffix,"p_nom"] == pytest.approx(capacity)
        assert loaded.generators.at[country+" vent","p_nom"] == pytest.approx(18)
        assert loaded.generators.at[country+" vent","marginal_cost"] == -.02
    assert len(loaded.meta["low_voltage_capacity_adjustments"]) == 4
    assert loaded.meta["heat_vent_capacity"]["mode"] == "dispatch"
    assert loaded.meta["interconnector_outages"][0]["hours"] == 3
    assert (loaded.links_t.p_max_pu.loc[loaded.snapshots[3:6],"cable"] == 0).all()
