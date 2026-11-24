"""Supply-only selection, dates, scenario isolation and rolling behavior."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.fuel_generator_disruption import (
    apply_fuel_generator_disruptions, fuel_generator_disruption_report,
)
from scripts.run_disruption_campaign import campaign_tasks, load_config, NEW_YEARS
from scripts.run_fuel_supply_campaign import configfiles


def network():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range('2014-01-01', periods=8, freq='h'))
    for name, carrier, country in [('DK gas', 'gas', 'DK'), ('DE gas', 'gas', 'DE'),
                                    ('EU oil primary', 'oil primary', 'EU'),
                                    ('EU oil', 'oil', 'EU'), ('power', 'AC', 'DK'),
                                    ('heat', 'urban central heat', 'DK')]:
        n.add('Bus', name, carrier=carrier, country=country, location=country)
    n.add('Generator', 'DK supply', bus='DK gas', carrier='gas', p_nom=10, p_max_pu=0.8)
    n.add('Generator', 'DE supply', bus='DE gas', carrier='gas', p_nom=20)
    n.add('Generator', 'EU oil primary', bus='EU oil primary', carrier='oil primary', p_nom=30)
    n.add('Generator', 'biogas supply', bus='DK gas', carrier='biogas', p_nom=5)
    n.add('Generator', 'gas-labelled electrical generator', bus='power', carrier='gas', p_nom=5)
    n.add('Generator', 'demand shedding', bus='DK gas', carrier='load-shedding-fuel-gas', p_nom=5)
    n.add('Link', 'pipeline', bus0='DE gas', bus1='DK gas', carrier='gas pipeline', p_nom=4, p_min_pu=-1)
    n.add('Link', 'CHP', bus0='DK gas', bus1='power', bus2='heat',
          carrier='urban central gas CHP', p_nom=7, efficiency=0.4, efficiency2=0.5)
    n.add('Link', 'refinery', bus0='EU oil primary', bus1='EU oil', carrier='oil refining', p_nom=30)
    n.add('Store', 'gas inventory', bus='DK gas', carrier='gas', e_nom=10, e_initial=5)
    return n


def event(n, fuel='gas', reduction=0.5, **kwargs):
    return {'name': fuel, 'fuel': fuel, 'reduction': reduction,
            'start': str(n.snapshots[2]), 'end': str(n.snapshots[5]), **kwargs}


@pytest.mark.parametrize('fuel,names', [('gas', ['DK supply', 'DE supply']), ('oil', ['EU oil primary'])])
@pytest.mark.parametrize('reduction', [0.5, 0.75, 0.9])
def test_only_supply_availability_changes_and_recovers(fuel, names, reduction):
    n = network()
    static = {c: n.components[c].static.copy(deep=True) for c in ['Generator', 'Link', 'Store']}
    normal = n.get_switchable_as_dense('Generator', 'p_max_pu').copy()
    link_dynamic = {k: v.copy(deep=True) for k,v in n.links_t.items()}
    apply_fuel_generator_disruptions(n, [event(n, fuel, reduction)])
    after = n.get_switchable_as_dense('Generator', 'p_max_pu')
    expected = normal.copy()
    expected.loc[n.snapshots[2:5], names] *= 1-reduction
    pd.testing.assert_frame_equal(after, expected)
    for component, table in static.items():
        pd.testing.assert_frame_equal(n.components[component].static, table)
    for key, frame in link_dynamic.items():
        pd.testing.assert_frame_equal(n.links_t[key], frame)
    report = fuel_generator_disruption_report(n)
    assert set(report.generator) == set(names)
    assert report.hours.eq(3).all()
    np.testing.assert_allclose(report.remaining_fraction, 1-reduction)


def test_country_and_exact_name_selection_and_simultaneous_different_fuels():
    n = network()
    apply_fuel_generator_disruptions(n, [event(n, countries=['DK']), event(n, 'oil', .75)])
    assert set(fuel_generator_disruption_report(n).generator) == {'DK supply', 'EU oil primary'}
    n = network()
    apply_fuel_generator_disruptions(n, [event(n, generators=['DE supply'])])
    assert fuel_generator_disruption_report(n).generator.tolist() == ['DE supply']


@pytest.mark.parametrize('change', [
    {'reduction': -0.1}, {'reduction': 1.1}, {'reduction': True},
    {'reduction': float('nan')}, {'start': '2000-01-01'},
    {'generators': ['gas-labelled electrical generator']},
    {'generators': ['biogas supply']}, {'generators': ['demand shedding']},
    {'generators': ['pipeline']}, {'countries': ['ZZ']},
    {'fuel': 'oil', 'countries': ['DK']}, {'links': ['pipeline']},
])
def test_invalid_selection_rejected_before_mutation(change):
    n = network()
    before = n.generators_t.p_max_pu.copy(deep=True)
    with pytest.raises(ValueError):
        apply_fuel_generator_disruptions(n, [event(n, **change)])
    pd.testing.assert_frame_equal(before, n.generators_t.p_max_pu)
    assert 'fuel_generator_disruptions' not in n.meta


def test_overlap_repeat_and_must_run_conflicts_are_rejected():
    n = network()
    with pytest.raises(ValueError, match='Overlapping'):
        apply_fuel_generator_disruptions(n, [event(n), {**event(n), 'name':'second'}])
    apply_fuel_generator_disruptions(n, [event(n)])
    with pytest.raises(ValueError, match='already applied'):
        apply_fuel_generator_disruptions(n, [event(n)])
    n = network()
    n.generators.at['DK supply', 'p_min_pu'] = .7
    with pytest.raises(ValueError, match='must-run'):
        apply_fuel_generator_disruptions(n, [event(n)])


def test_netCDF_retains_events_and_dated_availability(tmp_path):
    n = network()
    apply_fuel_generator_disruptions(n, [event(n)])
    path = tmp_path / 'disrupted.nc'
    n.export_to_netcdf(path)
    loaded = pypsa.Network(path)
    expected = n.generators_t.p_max_pu
    actual = loaded.generators_t.p_max_pu
    # NetCDF normalizes timestamp resolution and string index storage types.
    assert expected.index.tolist() == actual.index.tolist()
    assert expected.columns.tolist() == actual.columns.tolist()
    np.testing.assert_allclose(expected.to_numpy(), actual.to_numpy())
    pd.testing.assert_frame_equal(fuel_generator_disruption_report(n), fuel_generator_disruption_report(loaded))


def test_winter_campaign_has_28_independent_tasks_and_same_weather_years():
    root = Path(__file__).resolve().parents[1]
    config = load_config([root/'config/config.fuel_supply_disruption_2025.yaml',
                          root/'config/config.fuel_supply_winter_test_2025.yaml'])
    original = deepcopy(config)
    tasks = campaign_tasks(config, NEW_YEARS)
    assert len(tasks) == 28
    assert len({t['target'] for t in tasks}) == 28
    assert set(t['year'] for t in tasks) == {'1965-1966','1968-1969','1988-1989','2019-2020'}
    for task in tasks:
        settings = task['settings']
        case = settings['cases'][task['case']]
        assert case['interconnector_outages'] == []
        assert case['weather_network'] == settings['weather_years'][task['year']]['weather_network']
        assert settings['rolling_horizon']['storage']['initial_inventory'] == []
        assert settings['rolling_horizon']['simulation'] == {'start':'2014-01-01 00:00','end':'2014-01-29 00:00'}
        assert settings['fuel_backup']['enable'] is False
        assert settings['rolling_horizon']['resources']['import_capacity_MW'] is None
        assert config['solving']['options']['rolling_horizon'] is True
        events = case['fuel_generator_disruptions']
        if task['case'] == 'base_weather':
            assert events == []
        else:
            assert events[0]['reduction'] in [.5,.75,.9]
            assert pd.Timestamp(events[0]['end'])-pd.Timestamp(events[0]['start']) == pd.Timedelta(days=14)
    assert config == original
    assert configfiles()[-1].endswith('winter_test_2025.yaml')
    assert len(configfiles(full_year=True)) == 2
    assert configfiles(sophia=True)[1] == 'config/config.sophia.yaml'


def test_supply_shortage_propagates_through_unchanged_pipeline_and_rolling_windows():
    from scripts.rolling_disruption_dispatch import solve_rolling_dispatch
    from scripts import solve_network as solver
    n = pypsa.Network()
    n.set_snapshots(pd.date_range('2014-01-01', periods=8, freq='h'))
    n.add('Bus', 'entry', carrier='gas')
    n.add('Bus', 'consumer', carrier='gas')
    n.add('Load', 'fuel demand', bus='consumer', p_set=8)
    n.add('Generator', 'gas inlet', bus='entry', carrier='gas', p_nom=10, marginal_cost=10)
    n.add('Generator', 'unmet demand', bus='consumer', carrier='load-shedding-fuel-gas', p_nom=8, marginal_cost=1000)
    n.add('Link', 'pipeline', bus0='entry', bus1='consumer', carrier='gas pipeline', p_nom=20)
    assert n.optimize(solver_name='highs', log_to_console=False) == ('ok','optimal')
    n.model.solver_model = None
    reference = n.copy()
    apply_fuel_generator_disruptions(n, [event(n, reduction=.5)])
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root/'config/config.default.yaml').read_text())
    config['solving']['solver'] = {'name':'highs','options':''}
    model, solve = solver.collect_kwargs(config, config['solving'], [2025], mode='single')
    solve['log_to_console'] = False
    solve_rolling_dispatch(n, reference, config, SimpleNamespace(custom_extra_functionality=None),
                          model, solve, 2025, {'horizon':4,'overlap':2,
                          'resources':{'apply_scarcity_prices':False},
                          'storage':{'msv':{'enable':False}}})
    np.testing.assert_allclose(n.generators_t.p['gas inlet'], [8,8,5,5,5,8,8,8])
    np.testing.assert_allclose(n.generators_t.p['unmet demand'], [0,0,3,3,3,0,0,0])
    assert n.links.at['pipeline','p_nom'] == 20
    assert n.generators.at['gas inlet','p_nom'] == 10
