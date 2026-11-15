"""Small regressions for period waste treatment and hourly reference storage bids."""
import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.disruption_waste_hvc import prepare_waste_hvc
from test.test_rolling_disruption_dispatch import run, solve


def test_waste_buffer_crosses_windows_but_not_calendar_deadlines():
    ref = pypsa.Network()
    ref.set_snapshots(pd.date_range("2025-01-01", periods=8, freq="h"))
    for carrier in ("heat", "co2", "non-sequestered HVC"):
        ref.add("Carrier", carrier)
    ref.add("Bus", "heat", carrier="heat")
    ref.add("Bus", "products", carrier="non-sequestered HVC")
    ref.add("Bus", "atmosphere", carrier="co2")
    ref.add("Store", "products", bus="products", carrier="non-sequestered HVC", e_nom=8, e_initial=8)
    ref.add("Store", "atmosphere", bus="atmosphere", carrier="co2", e_nom=100)
    ref.add("Load", "heat demand", bus="heat", p_set=[0.25, 0.25, 1, 2.5] * 2)
    ref.add("Generator", "backup", bus="heat", p_nom=10, marginal_cost=10000)
    ref.add("Link", "waste heat", bus0="products", bus1="heat", bus2="atmosphere",
            p_nom=2.5, efficiency=1, efficiency2=2)
    ref.add("Link", "release", bus0="products", bus1="atmosphere", carrier="HVC to air",
            p_nom=0, efficiency=2)
    solve(ref)
    n = ref.copy()
    prepare_waste_hvc(n, {"treatment_period_hours": 4, "backlog_hours": 4})
    assert n.stores_t.e_max_pu.products.tolist() == [1, 1, 1, 0, 1, 1, 1, 0]
    assert n.links.at["release", "p_nom"] == 5
    reports = run(n, ref, options={"horizon": 5, "overlap": 2})
    assert np.allclose(n.generators_t.p.backup, 0)
    assert n.stores_t.e.products.iloc[2] == pytest.approx(1.5)
    assert np.allclose(n.stores_t.e.products.iloc[[3, 7]], 0)
    assert n.stores_t.e.products.min() >= -1e-8
    assert n.links_t.p0["waste heat"].iloc[3] == pytest.approx(2.5)
    assert n.links.at["waste heat", "p_nom"] == 2.5
    assert n.generators_t.p["products material arrivals"].sum() == pytest.approx(8)
    assert reports["dispatch_summary"].net_emissions_tCO2.item() == pytest.approx(16)


@pytest.mark.parametrize("aggregation", ["annual_mean", "hourly"])
def test_hourly_store_bid_changes_dispatch_and_reports_exact_profile(aggregation):
    ref = pypsa.Network()
    ref.set_snapshots(pd.date_range("2025-07-01", periods=3, freq="h"))
    ref.add("Carrier", "heat")
    ref.add("Bus", "heat", carrier="heat")
    ref.add("Load", "demand", bus="heat", p_set=1)
    ref.add("Generator", "supply", bus="heat", p_nom=2, marginal_cost=[20, 21, 22])
    ref.add("Store", "seasonal", bus="heat", carrier="heat", e_nom=1, e_initial=1)
    solve(ref)
    ref.stores_t.mu_energy_balance["seasonal"] = [5, 40, 40]
    n = ref.copy()
    reports = run(n, ref, options={"horizon": 3, "overlap": 1, "storage": {"msv": {
        "enable": True, "aggregation": aggregation, "stores": {"assets": ["seasonal"]},
    }}})
    expected = [5, 40, 40] if aggregation == "hourly" else [85 / 3] * 3
    assert np.allclose(n.stores_t.marginal_cost.seasonal, expected)
    assert np.allclose(reports["storage_msv_timeseries"]["Store::seasonal"], expected)
    assert reports["storage_msv_timeseries"].snapshot.tolist() == list(ref.snapshots)
    assert reports["storage_msv"].aggregation.item() == aggregation
    assert np.allclose(n.stores_t.p.seasonal, [1, -1, 0] if aggregation == "hourly" else 0)
    assert reports["dispatch_summary"].MSV_charge_EUR.item() == pytest.approx(-35 if aggregation == "hourly" else 0)
    assert not n.meta["rolling_dispatch"]["separate_terminal_value"]
    assert ref.stores_t.mu_energy_balance.seasonal.tolist() == [5, 40, 40]


@pytest.mark.parametrize("period", [0, -1, 1.5, True])
def test_invalid_waste_period_fails(period):
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-01-01", periods=4, freq="h"))
    with pytest.raises(ValueError, match="positive integer"):
        prepare_waste_hvc(n, {"treatment_period_hours": period})


def test_mixed_msv_keeps_fuel_values_constant_and_seasonal_values_hourly():
    ref = pypsa.Network()
    ref.set_snapshots(pd.date_range("2025-07-01", periods=3, freq="h"))
    for carrier in ("gas", "urban central water pits"):
        ref.add("Carrier", carrier)
        ref.add("Bus", carrier, carrier=carrier)
        ref.add("Load", carrier, bus=carrier, p_set=1)
        ref.add("Generator", carrier, bus=carrier, p_nom=2, marginal_cost=[20, 21, 22])
        ref.add("Store", carrier, bus=carrier, carrier=carrier, e_nom=1, e_initial=1)
    solve(ref)
    for name in ref.stores.index:
        ref.stores_t.mu_energy_balance[name] = [5, 40, 40]
    n = ref.copy()
    reports = run(n, ref, options={"horizon": 3, "overlap": 1, "storage": {"msv": {
        "aggregation": "hourly", "aggregation_by_carrier": {"gas": "annual_mean"},
        "stores": {"carriers": ["gas", "urban central water pits"]},
    }}})
    modes = reports["storage_msv"].set_index("asset").aggregation
    assert modes.gas == "annual_mean"
    assert modes["urban central water pits"] == "hourly"
    assert np.allclose(reports["storage_msv_timeseries"]["Store::gas"], 85 / 3)
    assert reports["storage_msv_timeseries"]["Store::urban central water pits"].tolist() == [5, 40, 40]
    assert np.allclose(n.stores_t.p.gas, 0)
    assert np.allclose(n.stores_t.p["urban central water pits"], [1, -1, 0])
    assert reports["dispatch_summary"].MSV_charge_EUR.item() == pytest.approx(-35)
    assert not n.meta["rolling_dispatch"]["resource_allowances_enforced"]
