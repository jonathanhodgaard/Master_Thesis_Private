"""Small hourly dispatches, never production disruption/capacity optimizations."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.resource_budget_duals import save_resource_budget_duals
from scripts.disruption_resource_budgets import build_resource_budgets
from scripts.disruption_storage_msv import apply_annual_msv, add_msv_charging_credit, msv_cost
from scripts.disruption_waste_hvc import prepare_waste_hvc
from scripts.rolling_disruption_dispatch import solve_rolling_dispatch


def configuration():
    path = Path(__file__).resolve().parents[1] / "config/config.default.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["solving"]["solver"] = {"name": "highs", "options": ""}
    return config


def solve(n):
    assert n.optimize(solver_name="highs", log_to_console=False, assign_all_duals=True) == ("ok", "optimal")
    n.model.solver_model = None
    return n


def fuel_reference(tmp_path, inventory=False):
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-07-01", periods=8, freq="h"))
    for carrier in ("gas", "unsustainable biogas", "solid biomass import"):
        n.add("Carrier", carrier)
    n.add("Bus", "fuel", carrier="gas")
    n.add("Load", "demand", bus="fuel", p_set=5)
    n.add("Generator", "fossil", bus="fuel", carrier="gas", p_nom=10, marginal_cost=10)
    if inventory:
        n.add("Bus", "imports", carrier="solid biomass import")
        n.add("Store", "import stock", bus="imports", carrier="solid biomass import",
              e_nom=8, e_initial=8, marginal_cost=2)
        n.add("Link", "import delivery", bus0="imports", bus1="fuel", p_nom=10, efficiency=1)
    else:
        n.add("Generator", "bio", bus="fuel", carrier="unsustainable biogas",
              p_nom=10, marginal_cost=2, e_sum_min=0, e_sum_max=8)
    solve(n)
    save_resource_budget_duals(n, tmp_path / "budgets.csv")
    return n


def run(n, ref, enforce=False, options=None):
    config = configuration()
    from scripts import solve_network as solver
    model_kwargs, solve_kwargs = solver.collect_kwargs(config, config["solving"], [2025], mode="single")
    solve_kwargs["log_to_console"] = False
    settings = {
        "horizon": 4, "overlap": 2,
        "resources": {"apply_scarcity_prices": True, "enforce_allowances": enforce},
        "storage": {"msv": {"enable": False}},
        "co2": {"replace_budget_with_price": True},
    }
    settings.update(options or {})
    return solve_rolling_dispatch(n, ref, config, SimpleNamespace(custom_extra_functionality=None),
                                  model_kwargs, solve_kwargs, 2025, settings)


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("inventory", [False, True])
def test_price_only_and_enforced_allowance_with_overlap(tmp_path, enforce, inventory):
    ref = fuel_reference(tmp_path, inventory=inventory)
    n = ref.copy()
    n.generators.at["fossil", "marginal_cost"] = 20
    reports = run(n, ref, enforce=enforce)
    usage = reports["resource_usage"]
    expected = 8 if enforce else 40
    assert usage.implemented_MWh.sum() == pytest.approx(expected)
    assert usage.remaining_MWh.iloc[-1] == pytest.approx(8 - expected)
    assert usage.scarcity_price_EUR_per_MWh.iloc[0] == pytest.approx(8)
    assert reports["window_summary"].implemented_hours.sum() == 8
    assert not n.generators_t.p.index.duplicated().any()
    assert reports["dispatch_summary"].operating_cost_EUR.item() == pytest.approx(2 * expected + 20 * (40 - expected))
    assert reports["dispatch_summary"].resource_charge_EUR.item() == pytest.approx(8 * expected)
    assert not n.meta["rolling_dispatch"]["separate_terminal_value"]
    n.export_to_netcdf(tmp_path / f"rolling_{inventory}_{enforce}.nc")
    loaded = pypsa.Network(tmp_path / f"rolling_{inventory}_{enforce}.nc")
    assert len(loaded.snapshots) == 8


def test_stale_reference_and_missing_duals_fail(tmp_path):
    ref = fuel_reference(tmp_path)
    n = ref.copy()
    del ref.meta["resource_budget_duals"]
    with pytest.raises(ValueError, match="Missing exported"):
        build_resource_budgets(n, ref, {}, ref.snapshots[0])
    ref.generators.at["bio", "e_sum_min"] = 8
    with pytest.raises(ValueError, match="compulsory"):
        build_resource_budgets(n, ref, {}, ref.snapshots[0])


def test_resource_override_and_interior_start(tmp_path):
    ref = fuel_reference(tmp_path)
    n = ref.copy()
    n.generators.at["fossil", "marginal_cost"] = 20
    prior = ref.generators_t.p.bio.iloc[:2].sum()
    reports = run(n, ref, options={
        "simulation": {"start": str(ref.snapshots[2])},
        "resources": {"enforce_allowances": False, "apply_scarcity_prices": True,
                      "overrides": {"unsustainable biogas": {"enforce_allowance": True}}},
    })
    assert reports["resource_usage"].implemented_MWh.sum() == pytest.approx(8 - prior)
    assert reports["resource_usage"].used_before_run_MWh.iloc[0] == pytest.approx(prior)
    assert len(n.snapshots) == 6


def hydro_network(charging=False):
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-07-01", periods=4, freq="h"))
    n.add("Carrier", "AC")
    n.add("Carrier", "hydro")
    n.add("Bus", "power", carrier="AC")
    n.add("Generator", "grid", bus="power", p_nom=10, marginal_cost=1)
    n.add("Load", "demand", bus="power", p_set=0.5)
    n.add("StorageUnit", "reservoir", bus="power", carrier="hydro", p_nom=2, max_hours=4,
          efficiency_dispatch=0.5, efficiency_store=0.8, marginal_cost=1,
          inflow=0 if charging else 2, p_dispatch_set=0 if charging else 0.5,
          p_store_set=1 if charging else 0)
    if not charging:
        n.storage_units_t.state_of_charge_set["reservoir"] = 0
    return solve(n)


@pytest.mark.parametrize("charging", [False, True])
def test_hydro_msv_efficiency_spill_and_charging_credit(charging):
    ref = hydro_network(charging)
    ref.storage_units_t.mu_energy_balance["reservoir"] = [6, 6, 18, 18]
    n = ref.copy()
    values, original = apply_annual_msv(n, ref, {"storage_units": {"carriers": ["hydro"]}})
    assert values["StorageUnit"].reservoir == 12
    assert original["StorageUnit", "marginal_cost"].reservoir.iloc[0] == 1
    assert n.storage_units_t.marginal_cost.reservoir.iloc[0] == 25
    n.optimize.create_model()
    add_msv_charging_credit(n, n.snapshots, values)
    assert n.optimize.solve_model(solver_name="highs", log_to_console=False) == ("ok", "optimal")
    assert msv_cost(n, n.snapshots, values) == pytest.approx(-4 * 12 * 0.8 if charging else 4 * 12 * 2)
    with pytest.raises(ValueError, match="already"):
        apply_annual_msv(n, ref, {})


def test_store_msv_selection_and_full_reference_average():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-07-01", periods=4, freq="h"))
    n.add("Carrier", "AC")
    n.add("Bus", "power", carrier="AC")
    n.add("Generator", "grid", bus="power", p_nom=10, marginal_cost=10)
    n.add("Load", "load", bus="power", p_set=1)
    n.add("Store", "seasonal", bus="power", carrier="urban central water pits", e_nom=10, e_cyclic=True)
    n.add("Store", "battery", bus="power", carrier="battery", e_nom=2, e_cyclic=True)
    solve(n)
    n.stores_t.mu_energy_balance["seasonal"] = [2, 2, 10, 10]
    ref = n.copy()
    values, _ = apply_annual_msv(n, ref, {"stores": {"carriers": ["urban central water pits"]}})
    assert values["Store"].seasonal == 6
    assert "battery" not in values["Store"]
    assert n.stores_t.marginal_cost.seasonal.eq(6).all()
    reports = run_store_continuity(ref)
    assert reports["window_summary"].implemented_hours.sum() == 4


def run_store_continuity(ref):
    n = ref.copy()
    reports = run(n, ref, options={"horizon": 3, "overlap": 1})
    for name in n.stores.index:
        previous = n.stores_t.e[name].shift(1)
        previous.iloc[0] = n.stores.at[name, "e_initial"]
        assert np.allclose(n.stores_t.e[name], previous - n.stores_t.p[name])
    return reports


def test_waste_hvc_arrivals_preserve_carbon_and_true_deadline():
    ref = pypsa.Network()
    ref.set_snapshots(pd.date_range("2025-07-01", periods=4, freq="h"))
    for name, carrier in (("waste", "municipal solid waste"), ("hvc", "non-sequestered HVC"), ("air", "co2")):
        ref.add("Carrier", carrier)
        ref.add("Bus", name, carrier=carrier)
    ref.add("Generator", "waste supply", bus="waste", carrier="municipal solid waste", p_nom=4,
            e_sum_min=4, e_sum_max=4, marginal_cost=0.1)
    ref.add("Store", "products", bus="hvc", carrier="non-sequestered HVC", e_nom=4, e_initial=4)
    ref.stores_t.e_max_pu["products"] = [1, 1, 1, 0]
    ref.add("Store", "atmosphere", bus="air", carrier="co2", e_nom=np.inf, e_min_pu=-1)
    ref.add("Link", "waste transfer", bus0="waste", bus1="hvc", bus2="air",
            carrier="municipal solid waste", p_nom=10, efficiency=1, efficiency2=-2)
    ref.add("Link", "release", bus0="hvc", bus1="air", carrier="HVC to air", p_nom=10,
            efficiency=2, marginal_cost=0.01)
    solve(ref)
    n = ref.copy()
    prepare_waste_hvc(n, {"backlog_hours": 2, "require_empty_at_period_end": True})
    assert ref.stores.at["products", "e_initial"] == 4
    reports = run(n, ref)
    assert n.stores_t.e.products.iloc[-1] == pytest.approx(0)
    assert reports["dispatch_summary"].net_emissions_tCO2.item() == pytest.approx(8)
    assert n.generators_t.p["waste supply"].eq(1).all()


def test_co2_budget_becomes_price_without_changing_sequestration():
    from scripts.disruption_resource_budgets import replace_co2_budget
    n = pypsa.Network()
    n.add("GlobalConstraint", "CO2Limit", type="co2_atmosphere", sense="<=", constant=10)
    n.global_constraints.at["CO2Limit", "mu"] = -50
    n.add("GlobalConstraint", "sequestration", type="operational_limit", carrier_attribute="co2 sequestered", sense=">=", constant=0)
    ref = n.copy()
    assert replace_co2_budget(n, ref, True) == 50
    assert "CO2Limit" not in n.global_constraints.index
    assert "sequestration" in n.global_constraints.index


def test_household_shares_remain_fixed_in_every_window():
    from scripts.household_heat_constraints import META_KEY, add_household_heat_constraints
    ref = pypsa.Network()
    ref.set_snapshots(pd.date_range("2025-07-01", periods=4, freq="h"))
    ref.add("Carrier", "heat")
    for bus in ("demand heat", "a", "b"):
        ref.add("Bus", bus, carrier="heat")
    ref.add("Load", "heat load", bus="demand heat", p_set=10)
    groups = []
    for name, price in (("a", 1), ("b", 20)):
        ref.add("Generator", name + " heater", bus=name, p_nom=20, marginal_cost=price)
        ref.add("Link", name + " delivery", bus0=name, bus1="demand heat", p_nom=20)
        groups.append({"group": name, "heat_bus": "demand heat", "delivery": name + " delivery",
                       "demand_loads": ["heat load"]})
    ref.meta[META_KEY] = {"mode": "dispatch", "shares": {"a": 0.4, "b": 0.6}, "groups": groups}
    assert ref.optimize(solver_name="highs", log_to_console=False,
                        extra_functionality=add_household_heat_constraints) == ("ok", "optimal")
    ref.model.solver_model = None
    n = ref.copy()
    run(n, ref, options={"horizon": 3, "overlap": 1})
    assert np.allclose(n.links_t.p0["a delivery"], 4)
    assert np.allclose(n.links_t.p0["b delivery"], 6)


def test_failed_window_stops_without_committing(tmp_path, monkeypatch):
    ref = fuel_reference(tmp_path)
    n = ref.copy()
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        return "warning", "infeasible"
    monkeypatch.setattr(type(n.optimize), "solve_model", fail)
    with pytest.raises(RuntimeError, match="window 0"):
        run(n, ref)
    assert len(calls) == 1
    assert "rolling_dispatch" not in n.meta


def test_zero_allowance_still_disables_source_in_price_only_mode(tmp_path):
    ref = fuel_reference(tmp_path)
    ref.generators.at["bio", "e_sum_max"] = 0
    ref.meta["resource_budget_duals"] = []
    n = ref.copy()
    n.generators.at["fossil", "marginal_cost"] = 20
    reports = run(n, ref)
    assert reports["resource_usage"].implemented_MWh.sum() == pytest.approx(0)
    assert reports["resource_usage"].enforced.all()


def test_invalid_settings_and_missing_seasonal_duals_fail():
    ref = hydro_network()
    n = ref.copy()
    with pytest.raises(ValueError, match="overlap"):
        run(n, ref, options={"horizon": 2, "overlap": 2})
    n.add("GlobalConstraint", "GasLimit", type="primary_energy", carrier_attribute="gas_usage", constant=100)
    with pytest.raises(ValueError, match="without a rolling adapter"):
        run(n, ref)
    ref.storage_units_t.mu_energy_balance = pd.DataFrame(index=ref.snapshots)
    with pytest.raises(ValueError, match="Missing reference"):
        apply_annual_msv(n, ref, {"storage_units": {"carriers": ["hydro"]}})


def test_initial_state_uses_predecessor_and_omitted_zero_columns():
    from scripts.disruption_storage_msv import initialize_storage
    ref = pypsa.Network()
    ref.set_snapshots(pd.date_range("2025-07-01", periods=4, freq="h"))
    ref.add("Bus", "heat")
    ref.add("Store", "seasonal", bus="heat", e_nom=10, e_cyclic=True)
    ref.add("Store", "zero", bus="heat", e_nom=0, e_cyclic=True)
    ref.stores_t.e["seasonal"] = [1, 2, 3, 4]
    n = ref.copy()
    initialize_storage(n, ref, ref.snapshots[0])
    assert n.stores.at["seasonal", "e_initial"] == 4
    assert n.stores.at["zero", "e_initial"] == 0
    initialize_storage(n, ref, ref.snapshots[2])
    assert n.stores.at["seasonal", "e_initial"] == 2
    assert not n.stores.e_cyclic.any()
