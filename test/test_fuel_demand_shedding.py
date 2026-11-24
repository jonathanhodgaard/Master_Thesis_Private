"""Fuel-demand relaxation, price hierarchy, original capacity and reporting checks."""
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.fuel_demand_shedding import add_fuel_demand_shedding, fuel_shedding_reports
from scripts.prepare_network_disruptions import add_load_shedding, add_fuel_backup_links
from scripts.disruption_resource_budgets import build_resource_budgets


def network():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range('2013-07-01', periods=2, freq='h'))
    n.add('Bus', 'fuel', carrier='biomass')
    n.add('Bus', 'industry', carrier='solid biomass for industry', location='DK0 0AC', country='DK')
    n.add('Bus', 'electricity', carrier='AC')
    n.add('Load', 'industry biomass', bus='industry', p_set=[4, 6])
    n.add('Load', 'power demand', bus='electricity', p_set=[0, 0])
    n.add('Generator', 'domestic biomass', bus='fuel', p_nom=20, marginal_cost=10)
    n.add('Link', 'industrial delivery', bus0='fuel', bus1='industry',
          carrier='solid biomass for industry', p_nom=3, efficiency=1)
    return n


def test_original_delivery_capacity_and_real_shortfall_are_preserved(tmp_path):
    n = network()
    n.snapshot_weightings.loc[:, :] = np.array([[1], [2]])
    # Reversing the former doubling must restore the precise original capacity.
    add_fuel_backup_links(n, {'enable': True})
    assert n.links.at['industrial delivery', 'p_nom'] == 6
    add_fuel_backup_links(n, {'enable': False})
    assert n.links.at['industrial delivery', 'p_nom'] == 3
    add_load_shedding(n, fuel={'enable': True, 'default_cost': 1000})
    name = 'industry biomass load shedding fuel'
    assert n.generators.at[name, 'bus'] == 'industry'
    assert not n.generators.loc[n.generators.carrier.str.startswith('load-shedding-fuel-'), 'bus'].eq('fuel').any()
    status, condition = n.optimize(solver_name='highs', log_to_console=False)
    assert (status, condition) == ('ok', 'optimal')
    np.testing.assert_allclose(n.links_t.p0['industrial delivery'], [3, 3])
    np.testing.assert_allclose(n.generators_t.p[name], [1, 3])
    report = fuel_shedding_reports(n)
    row = report['fuel_shedding_by_demand'].iloc[0]
    assert row.unserved_MWh == pytest.approx(7)
    assert row.demand_MWh == pytest.approx(16)
    assert row.served_MWh == pytest.approx(9)
    assert row.penalty_cost_EUR == pytest.approx(7000)
    assert row.shedding_hours == 3
    assert report['fuel_shedding_by_fuel'].iloc[0].unserved_MWh == pytest.approx(7)
    path = tmp_path / 'prepared.nc'
    n.export_to_netcdf(path)
    reloaded = pypsa.Network(path)
    pd.testing.assert_frame_equal(report['fuel_shedding_by_demand'],
                                 fuel_shedding_reports(reloaded)['fuel_shedding_by_demand'])


def test_cost_precedence_hourly_bounds_refresh_disable_and_no_heat_overlap():
    n = network()
    settings = {'enable': True, 'default_cost': 100,
                'costs_by_fuel': {'biomass': 200},
                'costs_by_carrier': {'solid biomass for industry': 300},
                'costs_by_load': {'industry biomass': 400},
                'regional_cost_adders': {'DK0 0AC': 5}}
    name = 'industry biomass load shedding fuel'
    for key, expected in [('costs_by_load', 405), ('costs_by_carrier', 305),
                          ('costs_by_fuel', 205), (None, 105)]:
        add_load_shedding(n, fuel=settings)
        assert n.generators.at[name, 'marginal_cost'] == expected
        assert len(n.meta['fuel_demand_shedding']) == 1
        assert n.generators.at[name, 'p_nom'] == 6
        np.testing.assert_allclose(n.generators_t.p_max_pu[name], [4/6, 1])
        if key:
            settings.pop(key)
    n.loads_t.p_set.loc[:, 'industry biomass'] = [2, 10]
    add_load_shedding(n, fuel=settings)
    assert n.generators.at[name, 'p_nom'] == 10
    np.testing.assert_allclose(n.generators_t.p_max_pu[name], [0.2, 1])
    add_load_shedding(n, fuel={'enable': False})
    assert name not in n.generators.index
    assert fuel_shedding_reports(n)['fuel_shedding_by_demand'].empty


@pytest.mark.parametrize('settings', [
    {'enable': True, 'default_cost': -1},
    {'enable': True, 'costs_by_fuel': {'biomass': float('nan')}},
    {'enable': True, 'costs_by_load': {'unknown': 1}},
    {'enable': True, 'regional_cost_adders': {'DK0 0AC': -100000}},
    {'enable': True, 'default_cost': True},
    {'enable': True, 'unexpected': 1},
])
def test_invalid_prices_and_settings_do_not_mutate_network(settings):
    n = network()
    add_fuel_demand_shedding(n, {'enable': True})
    before = n.generators.copy()
    with pytest.raises(ValueError):
        add_fuel_demand_shedding(n, settings)
    pd.testing.assert_frame_equal(before, n.generators)


def test_all_present_fuel_demands_selected_but_emission_and_electricity_loads_excluded():
    n = network()
    for carrier in ['gas for industry', 'land transport oil', 'kerosene for aviation',
                    'shipping oil', 'agriculture machinery oil', 'naphtha for industry',
                    'coal for industry', 'industry methanol', 'shipping methanol',
                    'H2', 'NH3', 'process emissions', 'low voltage']:
        n.add('Bus', carrier, carrier=carrier)
        n.add('Load', carrier + ' demand', bus=carrier, p_set=2)
    add_fuel_demand_shedding(n, {'enable': True})
    records = n.meta['fuel_demand_shedding']
    assert len(records) == 12  # Initial biomass demand plus 11 fuel uses.
    assert {'oil', 'biomass', 'gas', 'coal', 'methanol', 'hydrogen', 'ammonia'} == {r['fuel'] for r in records.values()}
    assert not {'process emissions', 'low voltage'}.intersection(r['bus'] for r in records.values())


def test_no_import_rate_override_preserves_links_and_original_shared_supply_limit():
    reference = pypsa.Network()
    reference.set_snapshots(pd.date_range('2013-07-01', periods=2, freq='h'))
    reference.add('Bus', 'imports', carrier='solid biomass import')
    reference.add('Store', 'import stock', bus='imports', carrier='solid biomass import',
                  e_nom=100, e_initial=100, marginal_cost=54)
    for region, capacity in [('DK', 3), ('DE', 4)]:
        reference.add('Bus', region, carrier='biomass')
        reference.add('Link', region + ' import', bus0='imports', bus1=region,
                      carrier='solid biomass import', p_nom=capacity)
    n = reference.copy()
    budgets = build_resource_budgets(n, reference, {'apply_scarcity_prices': False,
                                     'enforce_allowances': True, 'import_capacity_MW': None}, n.snapshots[0])
    assert n.links.p_nom.tolist() == [3, 4]
    assert n.generators.at['import stock rolling supply', 'p_nom'] == 7
    assert budgets[0].remaining_MWh == 100


def test_reports_count_only_saved_committed_snapshots():
    n = network()
    add_fuel_demand_shedding(n, {'enable': True})
    name = 'industry biomass load shedding fuel'
    n.generators_t.p = pd.DataFrame({name: [1, 3]}, index=n.snapshots)
    n.set_snapshots(n.snapshots[:1])
    reports = fuel_shedding_reports(n)
    assert reports['fuel_shedding_by_fuel'].iloc[0].unserved_MWh == 1
    assert len(reports['fuel_shedding_timeseries']) == 1


def test_campaigns_disable_old_capacity_increases_and_enable_shedding():
    root = Path(__file__).resolve().parents[1]
    for name in ['config.interconnector_disruption.yaml', 'config.disruption_dispatch_2035_w_V2G.yaml',
                 'config.disruption_dispatch_2035_wo_V2G.yaml']:
        path = root / 'config' / name
        config = yaml.safe_load(path.read_text(encoding='utf-8'))['disruption']
        assert config['fuel_backup']['enable'] is False
        assert config['load_shedding']['fuel']['enable'] is True
        assert config['rolling_horizon']['resources'].get('import_capacity_MW') is None
