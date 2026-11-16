"""Small checks for dispatch-only distribution-capacity sensitivities."""
import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.prepare_network_disruptions import (
    apply_low_voltage_capacity_multipliers, assert_fixed_capacities, add_load_shedding,
)


def distribution_network():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-01-01", periods=2, freq="h"))
    for carrier in ["AC", "low voltage", "electricity distribution grid"]:
        n.add("Carrier", carrier)
    for country, capacity in [("FI", 10), ("SE", 20), ("DK", 30)]:
        n.add("Bus", country+" AC", carrier="AC", country=country)
        n.add("Bus", country+" LV", carrier="low voltage", country=country)
        for suffix, source, destination in [("", " AC", " LV"), ("-reversed", " LV", " AC")]:
            n.add("Link", country+" grid"+suffix, bus0=country+source, bus1=country+destination,
                  carrier="electricity distribution grid", p_nom=capacity, efficiency=.97)
        n.add("Generator", country+" supply", bus=country+" AC", p_nom=100, marginal_cost=1)
        n.add("Load", country+" demand", bus=country+" LV", p_set=10.5 if country=="FI" else 1)
    return n


def test_scaling_both_directions_preserves_other_inputs_and_is_exported(tmp_path):
    n = distribution_network()
    original = n.copy()
    apply_low_voltage_capacity_multipliers(n, {"FI": 1.12, "SE": 1.006})
    for country, expected in [("FI", 11.2), ("SE", 20.12)]:
        for suffix in ["", "-reversed"]:
            name=country+" grid"+suffix
            for attr in ["p_nom", "p_nom_opt", "p_nom_min", "p_nom_max"]:
                assert n.links.at[name,attr] == pytest.approx(expected)
    pd.testing.assert_frame_equal(n.links.loc[["DK grid", "DK grid-reversed"]],
                                  original.links.loc[["DK grid", "DK grid-reversed"]])
    pd.testing.assert_series_equal(n.links.efficiency, original.links.efficiency)
    pd.testing.assert_frame_equal(n.generators, original.generators)
    pd.testing.assert_frame_equal(n.loads_t.p_set, original.loads_t.p_set)
    assert_fixed_capacities(n)
    assert len(n.meta["low_voltage_capacity_adjustments"]) == 4
    path=tmp_path/"capacity_trial.nc"
    n.export_to_netcdf(path)
    loaded=pypsa.Network(path)
    assert loaded.links.at["FI grid", "p_nom"] == pytest.approx(11.2)
    assert loaded.meta["low_voltage_capacity_adjustments"] == n.meta["low_voltage_capacity_adjustments"]
    with pytest.raises(ValueError, match="already"):
        apply_low_voltage_capacity_multipliers(n, {"FI":1.12})


def test_enlarged_grid_can_replace_shedding_without_adding_generation():
    base=distribution_network()
    enlarged=base.copy()
    apply_low_voltage_capacity_multipliers(enlarged, {"FI":1.12, "SE":1.006})
    for n in [base,enlarged]:
        add_load_shedding(n)
        assert n.optimize(solver_name="highs", log_to_console=False) == ("ok","optimal")
    name="FI LV load shedding electricity"
    np.testing.assert_allclose(base.generators_t.p[name],.8,atol=1e-7)
    np.testing.assert_allclose(enlarged.generators_t.p[name],0,atol=1e-7)
    pd.testing.assert_series_equal(base.generators.p_nom,enlarged.generators.p_nom)


@pytest.mark.parametrize("factor", [0,-1,float("nan"),float("inf"),True,"1.12"])
def test_invalid_multiplier_does_not_change_network(factor):
    n=distribution_network()
    before=n.links.copy()
    with pytest.raises(ValueError,match="positive finite"):
        apply_low_voltage_capacity_multipliers(n,{"FI":factor})
    pd.testing.assert_frame_equal(n.links,before)


def test_unmatched_country_fails_before_any_capacity_changes():
    n=distribution_network()
    before=n.links.copy()
    with pytest.raises(ValueError,match="No low-voltage"):
        apply_low_voltage_capacity_multipliers(n,{"FI":1.12,"XX":1.006})
    pd.testing.assert_frame_equal(n.links,before)


def test_no_override_preserves_default_and_extendable_grid_is_rejected():
    n=distribution_network()
    before=n.links.copy()
    apply_low_voltage_capacity_multipliers(n,{})
    pd.testing.assert_frame_equal(n.links,before)
    assert "low_voltage_capacity_adjustments" not in n.meta
    n.links.at["FI grid","p_nom_extendable"]=True
    with pytest.raises(ValueError,match="Freeze"):
        apply_low_voltage_capacity_multipliers(n,{"FI":1.12})
