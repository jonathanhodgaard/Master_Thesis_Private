from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pandas as pd
import pypsa
import pytest
import yaml

from scripts.fuel_demand_shedding import add_fuel_demand_shedding, fuel_shedding_reports
from scripts.hydrogen_balance_shedding import (
    add_hydrogen_balance_shedding, add_hydrogen_shedding_constraints,
    hydrogen_shedding_reports, CARRIER,
)


def network():
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2014-01-08',periods=2,freq='h'))
    n.add('Bus','gas',carrier='gas')
    n.add('Bus','H2',carrier='H2',location='SE0',country='SE')
    n.add('Bus','methanol',carrier='methanol')
    n.add('Generator','gas source',bus='gas',p_nom=1000,marginal_cost=10)
    n.add('Link','SMR',bus0='gas',bus1='H2',carrier='SMR',p_nom=501.24568,p_max_pu=.1,efficiency=.76)
    n.add('Link','methanolisation',bus0='H2',bus1='methanol',carrier='methanolisation',
          p_nom=383.229817,p_min_pu=.3,efficiency=1)
    n.add('Load','methanol requirement',bus='methanol',p_set=383.229817*.3)
    return n


def options(**extra):
    return {'enable':True,'default_cost':23300,'hydrogen_balance':{'enable':True},**extra}


def solve(n):
    return n.optimize(solver_name='highs',log_to_console=False,
        extra_functionality=add_hydrogen_shedding_constraints,primal_feasibility_tolerance=1e-9)


def test_swedish_forced_production_is_feasible_without_relaxing_minima(tmp_path):
    n=network()
    before=n.links.copy(deep=True)
    add_fuel_demand_shedding(n,options())
    pd.testing.assert_frame_equal(n.links,before)
    assert n.generators.at['H2 hydrogen balance shedding','marginal_cost']==23300
    assert 'H2 hydrogen balance shedding' in n.meta['hydrogen_balance_shedding']
    assert not any(r['fuel']=='hydrogen' for r in n.meta['fuel_demand_shedding'].values())
    assert solve(n)==('ok','optimal')
    deficit=383.229817*.3-501.24568*.1*.76
    np.testing.assert_allclose(n.generators_t.p['H2 hydrogen balance shedding'],deficit,atol=1e-7)
    assert n.links.at['methanolisation','p_min_pu']==.3
    report=hydrogen_shedding_reports(n)['hydrogen_shedding_by_bus'].iloc[0]
    assert report.missing_hydrogen_MWh==pytest.approx(2*deficit)
    assert report.penalty_cost_EUR==pytest.approx(2*deficit*23300)
    assert not (fuel_shedding_reports(n)['fuel_shedding_by_demand'].fuel=='hydrogen').any()
    path=tmp_path/'hydrogen.nc'
    n.export_to_netcdf(path)
    loaded=pypsa.Network(path)
    pd.testing.assert_frame_equal(hydrogen_shedding_reports(n)['hydrogen_shedding_by_bus'],
                                  hydrogen_shedding_reports(loaded)['hydrogen_shedding_by_bus'])


@pytest.mark.parametrize('extra,expected',[
    ({'default_cost':1234},1234),
    ({'costs_by_fuel':{'hydrogen':2000}},2000),
    ({'costs_by_carrier':{'H2':3000}},3000),
    ({'hydrogen_balance':{'enable':True,'cost':4000}},4000),
    ({'hydrogen_balance':{'enable':True,'costs_by_bus':{'H2':5000}},
      'regional_cost_adders':{'SE0':5}},5005),
])
def test_price_inheritance_overrides_refresh_and_disable(extra,expected):
    n=network()
    settings=options(**extra)
    add_fuel_demand_shedding(n,settings)
    add_fuel_demand_shedding(n,settings)
    assert len(n.generators[n.generators.carrier.eq(CARRIER)])==1
    assert n.generators.at['H2 hydrogen balance shedding','marginal_cost']==expected
    add_fuel_demand_shedding(n,{'enable':False})
    assert not n.generators.carrier.eq(CARRIER).any()


def test_cannot_create_hydrogen_only_to_charge_storage():
    n=network()
    n.links.at['methanolisation','p_min_pu']=0
    n.loads.at['methanol requirement','p_set']=0
    n.remove('Generator','gas source')
    # Charging this Store has a large economic credit, but cannot justify slack.
    n.add('Store','hydrogen stock',bus='H2',e_nom=100,e_initial=0,marginal_cost=50000)
    add_fuel_demand_shedding(n,options())
    assert solve(n)==('ok','optimal')
    np.testing.assert_allclose(n.generators_t.p['H2 hydrogen balance shedding'],0,atol=1e-8)
    np.testing.assert_allclose(n.stores_t.e['hydrogen stock'],0,atol=1e-8)


def test_secondary_hydrogen_input_and_transport_exclusion():
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2014-01-08',periods=2,freq='h'))
    for bus,carrier in [('biomass','biomass'),('methanol','methanol'),('H2','H2'),('other H2','H2')]:
        n.add('Bus',bus,carrier=carrier)
    n.add('Generator','biomass source',bus='biomass',p_nom=10)
    n.add('Link','biomethanol',bus0='biomass',bus1='methanol',bus2='H2',
          efficiency=1,efficiency2=[-.2,-.4],p_nom=10)
    n.add('Link','H2 pipeline',bus0='other H2',bus1='H2',p_nom=100,p_min_pu=-1)
    n.add('Load','methanol demand',bus='methanol',p_set=5)
    add_fuel_demand_shedding(n,options())
    assert len(n.meta['hydrogen_balance_shedding'])==1
    assert solve(n)==('ok','optimal')
    np.testing.assert_allclose(n.generators_t.p['H2 hydrogen balance shedding'],[1,2])


def test_final_hydrogen_shedding_and_process_shortfalls_stay_separate():
    n=network()
    n.add('Load','hydrogen for industry',bus='H2',p_set=5)
    add_fuel_demand_shedding(n,options())
    assert 'hydrogen for industry load shedding fuel' in n.meta['fuel_demand_shedding']
    assert 'H2 hydrogen balance shedding' in n.meta['hydrogen_balance_shedding']
    assert solve(n)==('ok','optimal')
    final=fuel_shedding_reports(n)['fuel_shedding_by_demand']
    balance=hydrogen_shedding_reports(n)['hydrogen_shedding_by_bus']
    total=final.loc[final.fuel.eq('hydrogen'),'unserved_MWh'].sum()+balance.missing_hydrogen_MWh.sum()
    assert total==pytest.approx(2*(5+383.229817*.3-501.24568*.1*.76))


def test_rolling_hydrogen_shedding_is_committed_once_and_preserves_minimums():
    from scripts.rolling_disruption_dispatch import solve_rolling_dispatch
    from scripts import solve_network as solver
    n=network()
    n.set_snapshots(pd.date_range('2014-01-08',periods=8,freq='h'))
    add_fuel_demand_shedding(n,options())
    assert solve(n)==('ok','optimal')
    n.model.solver_model=None
    ref=n.copy()
    root=Path(__file__).resolve().parents[1]
    config=yaml.safe_load((root/'config/config.default.yaml').read_text())
    config['solving']['solver']={'name':'highs','options':''}
    model,kwargs=solver.collect_kwargs(config,config['solving'],[2025],mode='single')
    kwargs['log_to_console']=False
    solve_rolling_dispatch(n,ref,config,SimpleNamespace(custom_extra_functionality=None),model,kwargs,2025,
        {'horizon':4,'overlap':2,'resources':{'apply_scarcity_prices':False},'storage':{'msv':{'enable':False}}})
    reports=hydrogen_shedding_reports(n)
    assert len(reports['hydrogen_shedding_timeseries'])==8
    assert reports['hydrogen_shedding_by_bus'].missing_hydrogen_MWh.sum()==pytest.approx(8*(383.229817*.3-501.24568*.1*.76))
    assert n.links.at['methanolisation','p_min_pu']==.3
