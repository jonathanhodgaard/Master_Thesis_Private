"""One consumption boundary; supply, transport and inventory stay available."""
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.fuel_link_disruption import apply_fuel_link_disruptions, consuming_links, fuel_link_disruption_report
from scripts.fuel_generator_disruption import apply_fuel_generator_disruptions
from scripts.run_disruption_campaign import campaign_tasks, load_config, NEW_YEARS
from scripts.run_fuel_supply_campaign import configfiles


def network():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range('2014-01-01',periods=8,freq='h'))
    for name,carrier,country in [('gas','gas','DK'),('gas2','gas','DE'),
            ('oil primary','oil primary','EU'),('oil','oil','EU'),('oil2','oil','DK'),
            ('power','AC','DK'),('heat','urban central heat','DK'),
            ('industry','gas for industry','DK'),('industry service','industrial service','DK'),
            ('travel','land transport oil','DK'),('H2','H2','DK')]:
        n.add('Bus',name,carrier=carrier,country=country,location=country)
    n.add('Generator','gas supply',bus='gas',carrier='gas',p_nom=100)
    n.add('Generator','oil supply',bus='oil primary',carrier='oil primary',p_nom=100)
    n.add('Link','pipeline',bus0='gas2',bus1='gas',carrier='gas pipeline',p_nom=100,p_min_pu=-1)
    n.add('Link','oil transport',bus0='oil',bus1='oil2',carrier='oil transport',p_nom=100)
    n.add('Link','refinery',bus0='oil primary',bus1='oil',carrier='oil refining',p_nom=100)
    n.add('Link','CCGT',bus0='gas',bus1='power',carrier='CCGT',p_nom=10,efficiency=.6)
    n.add('Link','CHP',bus0='gas',bus1='power',bus2='heat',carrier='urban central gas CHP',
          p_nom=10,p_max_pu=.8,efficiency=.4,efficiency2=.5)
    n.add('Link','SMR',bus0='gas',bus1='H2',carrier='SMR',p_nom=10)
    n.add('Link','industry delivery',bus0='gas',bus1='industry',carrier='gas for industry',p_nom=10)
    n.add('Link','downstream industry',bus0='industry',bus1='industry service',carrier='industry',p_nom=10)
    n.add('Link','oil power',bus0='oil',bus1='power',carrier='oil',p_nom=10)
    n.add('Link','road transport',bus0='oil',bus1='travel',carrier='land transport oil',p_nom=10)
    n.add('Store','gas stock',bus='gas',e_nom=20,e_initial=10)
    return n


def event(n,fuel='gas',reduction=.5,**kwargs):
    return {'name':fuel,'fuel':fuel,'reduction':reduction,
            'start':str(n.snapshots[2]),'end':str(n.snapshots[5]),**kwargs}


@pytest.mark.parametrize('fuel,names', [('gas',['CCGT','CHP','SMR','industry delivery']),
                                     ('oil',['oil power','road transport'])])
@pytest.mark.parametrize('reduction',[.5,.75,.9])
def test_exact_consumption_selection_and_single_factor(fuel,names,reduction):
    n=network()
    static={c:n.components[c].static.copy(deep=True) for c in ['Generator','Link','Store']}
    gen=n.get_switchable_as_dense('Generator','p_max_pu').copy()
    original=n.get_switchable_as_dense('Link','p_max_pu').copy()
    expected=original.copy()
    expected.loc[n.snapshots[2:5],names]*=1-reduction
    apply_fuel_link_disruptions(n,[event(n,fuel,reduction)])
    pd.testing.assert_frame_equal(n.get_switchable_as_dense('Link','p_max_pu'),expected)
    pd.testing.assert_frame_equal(n.get_switchable_as_dense('Generator','p_max_pu'),gen)
    for c,frame in static.items():
        pd.testing.assert_frame_equal(n.components[c].static,frame)
    assert set(fuel_link_disruption_report(n).link)==set(names)
    # CHP electric and heat outputs share ONE restricted input, not two factors.
    assert n.links.at['CHP','efficiency']==.4
    assert n.links.at['CHP','efficiency2']==.5


@pytest.mark.parametrize('change', [
    {'links':['pipeline']},{'links':['refinery']},{'links':['downstream industry']},
    {'reduction':-1},{'reduction':float('nan')},{'reduction':True},
    {'countries':['ZZ']},{'start':'2000-01-01'}, {'generators':['gas supply']},
])
def test_bad_or_upstream_selection_is_rejected_without_mutation(change):
    n=network()
    before=n.links_t.p_max_pu.copy(deep=True)
    with pytest.raises(ValueError):
        apply_fuel_link_disruptions(n,[event(n,**change)])
    pd.testing.assert_frame_equal(n.links_t.p_max_pu,before)


@pytest.mark.parametrize('supply_first',[False,True])
def test_cannot_reduce_upstream_and_consuming_links_for_same_fuel(supply_first):
    n=network()
    if supply_first:
        apply_fuel_generator_disruptions(n,[event(n)])
        with pytest.raises(ValueError,match='Cannot combine'):
            apply_fuel_link_disruptions(n,[event(n)])
    else:
        apply_fuel_link_disruptions(n,[event(n)])
        with pytest.raises(ValueError,match='Cannot combine'):
            apply_fuel_generator_disruptions(n,[event(n)])


def test_oil_country_attributed_to_consuming_region_and_overlap_rejected():
    n=network()
    apply_fuel_link_disruptions(n,[event(n,'oil',countries=['DK'])])
    assert set(fuel_link_disruption_report(n).link)=={'oil power','road transport'}
    n=network()
    with pytest.raises(ValueError,match='Overlapping'):
        apply_fuel_link_disruptions(n,[event(n),{**event(n),'name':'second'}])


def test_new_default_campaign_has_only_consuming_link_reductions():
    root=Path(__file__).resolve().parents[1]
    assert configfiles()[-2:] == ['config/config.fuel_use_disruption_2025.yaml',
                                'config/config.fuel_use_winter_test_2025.yaml']
    c=load_config([root/'config/config.fuel_use_disruption_2025.yaml',
                   root/'config/config.fuel_use_winter_test_2025.yaml'])
    tasks=campaign_tasks(c,NEW_YEARS)
    assert len(tasks)==28
    assert len({t['target'] for t in tasks})==28
    for task in tasks:
        case=task['settings']['cases'][task['case']]
        assert case['fuel_generator_disruptions']==[]
        assert case['interconnector_outages']==[]
        if task['case']=='base_weather':
            assert case['fuel_link_disruptions']==[]
        else:
            assert case['fuel_link_disruptions'][0]['reduction'] in [.5,.75,.9]
        assert 'fuel_use_winter_test' in task['target']


def test_rolling_industry_shortfall_with_original_supply_and_pipeline():
    from scripts.fuel_demand_shedding import add_fuel_demand_shedding, fuel_shedding_reports
    from scripts.rolling_disruption_dispatch import solve_rolling_dispatch
    from scripts import solve_network as solver
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2014-01-01',periods=8,freq='h'))
    n.add('Bus','entry',carrier='gas')
    n.add('Bus','gas',carrier='gas')
    n.add('Bus','industry',carrier='gas for industry')
    n.add('Generator','gas supply',bus='entry',carrier='gas',p_nom=100,marginal_cost=10)
    n.add('Link','pipeline',bus0='entry',bus1='gas',carrier='gas pipeline',p_nom=100)
    n.add('Link','delivery',bus0='gas',bus1='industry',carrier='gas for industry',p_nom=10)
    n.add('Load','industrial gas',bus='industry',p_set=8)
    add_fuel_demand_shedding(n,{'enable':True,'default_cost':1000})
    assert n.optimize(solver_name='highs',log_to_console=False)==('ok','optimal')
    n.model.solver_model=None
    reference=n.copy()
    apply_fuel_link_disruptions(n,[event(n)])
    root=Path(__file__).resolve().parents[1]
    c=yaml.safe_load((root/'config/config.default.yaml').read_text())
    c['solving']['solver']={'name':'highs','options':''}
    model,solve=solver.collect_kwargs(c,c['solving'],[2025],mode='single')
    solve['log_to_console']=False
    solve_rolling_dispatch(n,reference,c,SimpleNamespace(custom_extra_functionality=None),model,solve,2025,
        {'horizon':4,'overlap':2,'resources':{'apply_scarcity_prices':False},'storage':{'msv':{'enable':False}}})
    np.testing.assert_allclose(n.links_t.p0['delivery'],[8,8,5,5,5,8,8,8])
    np.testing.assert_allclose(n.generators_t.p['industrial gas load shedding fuel'],[0,0,3,3,3,0,0,0])
    assert n.generators.at['gas supply','p_nom']==100
    assert n.links.at['pipeline','p_nom']==100
    assert fuel_shedding_reports(n)['fuel_shedding_by_fuel'].iloc[0].unserved_MWh==pytest.approx(9)
