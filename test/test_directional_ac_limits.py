"""Small lossy AC solves and persistence checks; no production optimisation."""
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.directional_ac_limits import (
    META_KEY, prepare_directional_ac_limits, add_directional_ac_limit_constraints,
)
from test.test_rolling_disruption_dispatch import run
from test.test_capacity_overrides import apply_capacity_overrides

ROOT = Path(__file__).resolve().parents[1]
RULE = dict(name="oeresund", from_bus="DK0 0AC", to_bus="SE0 0AC",
            forward_mw=1700, reverse_mw=1300)


def network(direction="export", reversed_line=False):
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2013-07-01", periods=4, freq="h"))
    n.add("Carrier", "AC")
    for name, country in [("DK0 0AC", "DK"), ("SE0 0AC", "SE")]:
        n.add("Bus", name, carrier="AC", country=country, v_nom=380)
        n.add("Generator", name, bus=name, p_nom=5000, marginal_cost=100)
    source, sink = ("DK0 0AC", "SE0 0AC") if direction == "export" else ("SE0 0AC", "DK0 0AC")
    n.generators.at[source, "marginal_cost"] = 10
    n.add("Load", "demand", bus=sink, p_set=2500)
    ends = ("SE0 0AC", "DK0 0AC") if reversed_line else ("DK0 0AC", "SE0 0AC")
    n.add("Line", "4", bus0=ends[0], bus1=ends[1], s_nom=3574.952866822163,
          s_max_pu=0.7, x=20, r=2)
    original = n.lines.copy()
    prepare_directional_ac_limits(n, [RULE])
    pd.testing.assert_frame_equal(n.lines, original)
    return n


def solve(n):
    assert n.optimize(solver_name="highs", log_to_console=False,
                      transmission_losses=2,
                      extra_functionality=add_directional_ac_limit_constraints) == ("ok", "optimal")
    n.model.solver_model = None


@pytest.mark.parametrize("direction,expected", [("export", 1700), ("import", -1300)])
@pytest.mark.parametrize("reversed_line", [False, True])
def test_directional_exchange_limits_preserve_losses(direction, expected, reversed_line):
    n = network(direction, reversed_line)
    solve(n)
    orientation = -1 if reversed_line else 1
    transfer = n.model["Line-s"].solution.sel(name="4").to_pandas() * orientation
    np.testing.assert_allclose(transfer, expected, atol=1e-5)
    losses = n.lines_t.p0["4"] + n.lines_t.p1["4"]
    assert losses.gt(0).all()
    np.testing.assert_allclose(
        n.generators_t.p.sum(axis=1) - 2500, losses, atol=1e-5,
    )


def test_limits_survive_export_and_apply_in_every_rolling_window(tmp_path):
    ref = network()
    solve(ref)
    path = tmp_path / "base.nc"
    ref.export_to_netcdf(path)
    loaded = pypsa.Network(path)
    assert loaded.meta[META_KEY] == ref.meta[META_KEY]
    # Force imports in alternating hours to exercise both directions.
    loaded.remove("Load", "demand")
    loaded.add("Load", "swedish demand", bus="SE0 0AC", p_set=[2500, 0, 2500, 0])
    loaded.add("Load", "danish demand", bus="DK0 0AC", p_set=[0, 2500, 0, 2500])
    loaded.generators_t.marginal_cost["DK0 0AC"] = [10, 100, 10, 100]
    loaded.generators_t.marginal_cost["SE0 0AC"] = [100, 10, 100, 10]
    reports = run(loaded, ref, options={"horizon": 3, "overlap": 1})
    assert reports["window_summary"].implemented_hours.sum() == 4
    transfer = (loaded.lines_t.p0["4"] - loaded.lines_t.p1["4"]) / 2
    np.testing.assert_allclose(transfer, [1700, -1300, 1700, -1300], atol=1e-4)
    assert (loaded.lines_t.p0["4"] + loaded.lines_t.p1["4"]).gt(0).all()


def test_native_outage_still_forces_zero_transfer():
    n = network()
    n.lines_t.s_max_pu["4"] = [0.7, 0, 0.7, 0.7]
    solve(n)
    assert n.lines_t.p0["4"].iloc[1] == pytest.approx(0, abs=1e-6)
    assert n.lines_t.p1["4"].iloc[1] == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize("change", [dict(forward_mw=-1), dict(reverse_mw=float("nan")),
                                    dict(to_bus="unknown"), dict(forward_mw=True)])
def test_invalid_rules_fail(change):
    n = network()
    with pytest.raises(ValueError):
        prepare_directional_ac_limits(n, [{**RULE, **change}])


def test_ambiguous_or_duplicate_line_selection_fails():
    n = network()
    with pytest.raises(ValueError, match="Duplicate"):
        prepare_directional_ac_limits(n, [RULE, {**RULE, "name": "duplicate"}])
    n.add("Line", "parallel", bus0="DK0 0AC", bus1="SE0 0AC", x=20, s_nom=100)
    with pytest.raises(ValueError, match="exactly one"):
        prepare_directional_ac_limits(n, [RULE])


def test_old_base_cannot_silently_omit_configured_limits():
    n = network()
    n.meta.pop(META_KEY)
    n.config = {"sector": {"directional_ac_limits": [RULE]}}
    n.optimize.create_model()
    with pytest.raises(ValueError, match="rebuild"):
        add_directional_ac_limit_constraints(n, n.snapshots)


@pytest.mark.parametrize("sector,year", [("2025", 2025), ("2035_w_V2G", 2035), ("2035_wo_V2G", 2035)])
def test_three_base_configs_freeze_candidate_and_preserve_existing_gas_pipeline(sector, year):
    config = yaml.safe_load((ROOT / f"config/config.sector_{sector}.yaml").read_text(encoding="utf-8"))
    assert config["sector"]["directional_ac_limits"] == [RULE]
    assert config["solving"]["options"]["transmission_losses"] == 2
    n = network()
    for bus in ["DK0 0AC gas", "DK2 0AC gas"]:
        n.add("Bus", bus, carrier="gas")
    candidate = "gas pipeline new DK0 0AC <-> DK2 0AC"
    n.add("Link", candidate, carrier="gas pipeline new", bus0="DK0 0AC gas",
          bus1="DK2 0AC gas", p_nom=0, p_nom_extendable=True, p_min_pu=-1)
    n.add("Link", "existing gas pipeline", bus0="DK2 0AC gas", bus1="DK0 0AC gas",
          carrier="gas pipeline", p_nom=1500)
    rules = [r for r in config["sector"]["capacity_overrides"] if r["name"] == "freeze_new_zealand_gas_pipeline"]
    assert len(rules) == 1
    apply_capacity_overrides(n, rules, year)
    assert not n.links.at[candidate, "p_nom_extendable"]
    assert n.links.at[candidate, "p_nom"] == 0
    assert n.links.at["existing gas pipeline", "p_nom"] == 1500
