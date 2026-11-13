"""Small solved examples for optional fuel supply and persistent budget duals."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.resource_budget_duals import save_resource_budget_duals


def fuel_network(allowance=4, aggregate=False, minimum=0, bio_cost=2):
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-01-01", periods=2, freq="h"))
    n.add("Carrier", "unsustainable biogas")
    n.add("Carrier", "gas")
    n.add("Bus", "fuel", carrier="gas")
    n.add("Load", "demand", bus="fuel", p_set=5)
    n.add("Generator", "bio", bus="fuel", carrier="unsustainable biogas",
          p_nom=10, marginal_cost=bio_cost, e_sum_min=minimum,
          e_sum_max=np.inf if aggregate else allowance)
    n.add("Generator", "fossil", bus="fuel", carrier="gas", p_nom=10, marginal_cost=10)
    if aggregate:
        n.add("GlobalConstraint", "biogas allowance", type="operational_limit",
              carrier_attribute="unsustainable biogas", sense="<=", constant=allowance)
    status, condition = n.optimize(solver_name="highs", log_to_console=False)
    assert (status, condition) == ("ok", "optimal")
    return n


@pytest.mark.parametrize("aggregate", [False, True])
def test_dual_matches_relaxed_budget_and_survives_reload(tmp_path, aggregate):
    n = fuel_network(aggregate=aggregate)
    table = save_resource_budget_duals(n, tmp_path / "budgets.csv")
    row = table.loc[table.sense.eq("<=")].iloc[0]
    assert row.rhs_MWh == 4
    relaxed = fuel_network(allowance=4.01, aggregate=aggregate)
    assert relaxed.objective - n.objective == pytest.approx(row.raw_dual * 0.01)
    assert row.raw_dual == pytest.approx(-8)
    n.export_to_netcdf(tmp_path / "network.nc")
    loaded = pypsa.Network(tmp_path / "network.nc")
    assert loaded.meta["resource_budget_duals"] == n.meta["resource_budget_duals"]
    assert len(pd.read_csv(tmp_path / "budgets.csv")) == len(table)


def test_zero_allowance_and_nonbinding_budget(tmp_path):
    zero = fuel_network(allowance=0)
    result = save_resource_budget_duals(zero, tmp_path / "zero.csv")
    assert result.loc[result.sense.eq("<="), "rhs_MWh"].item() == 0
    unused = fuel_network(allowance=20)
    result = save_resource_budget_duals(unused, tmp_path / "unused.csv")
    assert result.loc[result.sense.eq("<="), "raw_dual"].item() == pytest.approx(0)


def test_optional_expensive_fuel_can_be_left_unused():
    optional = fuel_network(bio_cost=20)
    compulsory = fuel_network(bio_cost=20, minimum=4)
    assert optional.generators_t.p["bio"].sum() == pytest.approx(0)
    assert compulsory.generators_t.p["bio"].sum() == pytest.approx(4)
    assert optional.objective < compulsory.objective


def test_missing_dual_is_an_error(tmp_path):
    n = fuel_network()
    n.model.constraints["Generator-e_sum_max"].dual = np.nan
    with pytest.raises(ValueError, match="finite"):
        save_resource_budget_duals(n, tmp_path / "missing.csv")


@pytest.mark.parametrize("enforce", [True, False])
@pytest.mark.parametrize("aggregate", [True, False])
def test_preparation_preserves_allowances_and_waste(tmp_path, enforce, aggregate):
    # Load the production function without unrelated geospatial/download imports,
    # following test_capacity_overrides.py's isolated function-loading pattern.
    source = Path(__file__).resolve().parents[1] / "scripts/prepare_sector_network.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "add_biomass")
    class SupplyPrepared(Exception):
        pass
    n = pypsa.Network()
    original_add = n.add
    def add(component, name, **kwargs):
        result = original_add(component, name, **kwargs)
        if component == "GlobalConstraint" and name == "unsustainable biomass limit":
            raise SupplyPrepared
        return result
    def carrier_buses(*args, **kwargs):
        if not aggregate:
            raise SupplyPrepared
    namespace = {"pd": pd, "np": np, "logger": logging.getLogger(__name__),
                 "add_carrier_buses": carrier_buses}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    potentials = pd.DataFrame({
        "biogas": [3], "solid biomass": [7], "municipal solid waste": [2],
        "unsustainable biogas": [4], "unsustainable solid biomass": [8],
        "unsustainable bioliquids": [5],
    }, index=["XX"])
    potentials.to_csv(tmp_path / "potentials.csv")
    pd.DataFrame({"cost": [0.1, 0.2]}, index=["XX", "YY"]).to_csv(tmp_path / "transport.csv")
    spatial = SimpleNamespace(
        gas=SimpleNamespace(biogas=pd.Index(["XX biogas"]), locations=["XX"]),
        biomass=SimpleNamespace(nodes=pd.Index(["XX solid biomass"]), locations=["XX"],
                                nodes_unsustainable=pd.Index(["XX unsustainable solid biomass"]),
                                bioliquids=pd.Index(["XX unsustainable bioliquids"])),
        msw=SimpleNamespace(nodes=pd.Index(["XX municipal solid waste"]), locations=["XX"]),
        oil=SimpleNamespace(nodes=pd.Index(["XX oil"])),
    )
    costs = pd.DataFrame(1.0, index=["biogas", "solid biomass", "fuelwood", "biodiesel crops", "oil", "BtL"],
                         columns=["fuel", "VOM", "CO2 intensity"])
    options = {"gas_network": True, "biomass_spatial": True, "biomass_transport": False,
               "industry": True, "municipal_solid_waste": True,
               "solid_biomass_import": {"enable": False}, "biogas_upgrading": False,
               "biogas_upgrading_cc": False}
    n.add = add
    with pytest.raises(SupplyPrepared):
        namespace["add_biomass"](n, costs, options, spatial, {}, pd.DataFrame(),
                                 tmp_path / "potentials.csv", tmp_path / "transport.csv",
                                 enforce_minimum_use=enforce)
    fuels = n.generators.loc[n.generators.index.isin([
        "XX biogas unsustainable", "XX unsustainable solid biomass", "XX unsustainable bioliquids"
    ])]
    assert fuels.e_sum_max.tolist() == [4, 8, 5]
    assert n.generators.at["XX municipal solid waste", "e_sum_min"] == 2
    assert n.generators.at["XX municipal solid waste", "e_sum_max"] == 2
    if aggregate:
        assert n.global_constraints.at["unsustainable biomass limit", "constant"] == 8
        assert n.global_constraints.at["unsustainable biomass limit", "sense"] == ("==" if enforce else "<=")
    else:
        assert fuels.e_sum_min.tolist() == ([4, 8, 5] if enforce else [0, 0, 0])


def test_base_case_configuration():
    root = Path(__file__).resolve().parents[1]
    from scripts.lib.validation.config.biomass import BiomassConfig
    defaults = yaml.safe_load((root / "config/config.default.yaml").read_text(encoding="utf-8"))["biomass"]
    assert BiomassConfig(**defaults).enforce_minimum_use
    for scenario in ("2025", "2035_w_V2G", "2035_wo_V2G"):
        config = yaml.safe_load((root / f"config/config.sector_{scenario}.yaml").read_text(encoding="utf-8"))
        validated = BiomassConfig(**(defaults | config["biomass"]))
        assert not validated.enforce_minimum_use
        assert validated.export_budget_duals
