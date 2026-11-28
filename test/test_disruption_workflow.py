"""Small end-to-end campaign check, using temporary inputs and HiGHS only."""
import json
from pathlib import Path
import runpy

import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.disruption_scenarios import select_disruption, common_policy
from scripts.run_disruption_campaign import load_config, configfiles
from scripts.solve_disruption_dispatch import run
from scripts.analyse_disruption_windows import residual_load, select_campaign_windows, TIMING_METRICS
from scripts.apply_disruption import apply_scenario
from scripts.summarise_disruptions import metrics, plot_metrics


class Fields(dict):
    __getattr__ = dict.__getitem__


def test_prepare_baseline_timing_fuel_solve_and_plots(tmp_path, monkeypatch):
    import scripts.solve_disruption_dispatch as dispatch
    monkeypatch.setattr(dispatch, 'configure_logging', lambda smk: None)
    config=load_config(configfiles('2025','baseline'))
    config['solving']['solver']={'name':'highs','options':''}
    config['solving']['options']['transmission_losses']=False
    config['sector']['directional_ac_limits']=[]
    settings=select_disruption(config)
    settings['rolling_horizon'].update(horizon=4,overlap=1)
    settings['rolling_horizon']['storage']={'msv':{'enable':False}}
    settings['rolling_horizon']['resources']={'apply_scarcity_prices':False,'enforce_allowances':True}
    settings['base_network']=str(tmp_path/'reference.nc')
    settings['household_shares']=None
    config['disruption']=settings
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2013-07-01',periods=6,freq='h'))
    for name,carrier in [('DK0 0AC','AC'),('heat','urban central heat'),('gas','gas'),
                         ('import','solid biomass import'),('biomass','solid biomass')]:
        n.add('Bus',name,carrier=carrier,location='DK0 0AC',country='DK')
    n.add('Load','power',bus='DK0 0AC',p_set=[2,5,4,3,6,2])
    n.add('Load','heat',bus='heat',p_set=[1,1,1,1,1,5])
    n.add('Bus','SE0 0AC',carrier='AC',location='SE0 0AC',country='SE')
    n.add('Link','cable',bus0='SE0 0AC',bus1='DK0 0AC',carrier='DC',p_nom=2)
    n.add('Generator','gas supply',bus='gas',carrier='gas',p_nom=30,marginal_cost=10)
    n.add('Generator','solar',bus='DK0 0AC',carrier='solar',p_nom=2,p_max_pu=[0,.1,.7,.1,0,0])
    n.add('Link','CCGT',bus0='gas',bus1='DK0 0AC',carrier='CCGT',p_nom=20,efficiency=.5)
    n.add('Link','boiler',bus0='gas',bus1='heat',carrier='gas boiler',p_nom=10,efficiency=.9)
    n.add('Store','import stock',bus='import',carrier='solid biomass import',e_nom=100,e_initial=100)
    n.add('Link','biomass import',bus0='import',bus1='biomass',carrier='solid biomass import',p_nom=1)
    assert n.optimize(solver_name='highs',log_to_console=False,assign_all_duals=True)==('ok','optimal')
    n.model.solver_model=None
    n.export_to_netcdf(tmp_path/'reference.nc')
    policy=common_policy(settings,config['solving'],config)
    params=Fields(load_shedding=settings['load_shedding'],heat_vent=settings['heat_vent'],
                  biomass_import_capacity_MW=50000,rolling_horizon=True,rolling=settings['rolling_horizon'],policy=policy)
    smk=Fields(input=Fields(net=str(tmp_path/'reference.nc')),output=Fields(net=str(tmp_path/'prepared.nc')),params=params)
    root=Path(__file__).resolve().parents[1]
    runpy.run_path(str(root/'scripts/prepare_network_disruptions.py'),run_name='__main__',init_globals={'snakemake':smk})
    prepared=pypsa.Network(tmp_path/'prepared.nc')
    assert prepared.links.at['biomass import','p_nom']==50000
    assert prepared.links.at['CCGT','p_nom']==20

    def solve_case(name,network,baseline=False):
        directory=tmp_path/name
        directory.mkdir()
        outputs=Fields(network=str(directory/'network_solved.nc'),
                       **{key:str(directory/(key+'.csv')) for key in ['resource_usage','window_summary','dispatch_summary']})
        if baseline:
            outputs.update({key:str(directory/(key+'.csv')) for key in ['storage_msv','storage_msv_timeseries']})
        smk=Fields(config=config,input=Fields(network=str(network),reference=str(tmp_path/'reference.nc')),
                   output=outputs,log=Fields(solver=str(directory/'solver.log'),python=str(directory/'dispatch.log')),
                   params=Fields(solving=config['solving'],rolling=settings['rolling_horizon'],
                                 custom_extra_functionality=None,identity={'case':name}),rule='test')
        run(smk)
        assert json.loads((directory/'status.json').read_text())['phase']=='complete'
        assert not (directory/'failures').exists()
        assert not list(directory.glob('fuel*csv'))
        assert not (directory/'storage_opening.csv').exists()
        return pypsa.Network(outputs.network)

    baseline=solve_case('base_weather',tmp_path/'prepared.nc',True)
    hourly=residual_load(baseline,'DK0 0AC',['solar'])
    timing=Fields(input=Fields(network=str(tmp_path/'base_weather/network_solved.nc')),
                  output=Fields(hourly=str(tmp_path/'residual.csv'),windows=str(tmp_path/'events.csv')),
                  params=Fields(region='DK0 0AC',renewable_carriers=['solar'],durations=[1,2],
                                variant='2025',weather_year='2013-2014'))
    runpy.run_path(str(root/'scripts/analyse_disruption_windows.py'),run_name='__main__',init_globals={'snakemake':timing})
    windows=pd.read_csv(tmp_path/'events.csv',parse_dates=['start','end'])
    assert len(windows)==4
    assert set(windows.timing_metric)==set(TIMING_METRICS.values())
    heating=select_campaign_windows(windows,'fuel',[1,2])
    electric=select_campaign_windows(windows,'interconnector',[1,2])
    np.testing.assert_allclose(heating.residual_energy_MWh,
        heating.heat_demand_MWh - heating.renewable_available_MWh)
    np.testing.assert_allclose(electric.residual_energy_MWh,
        electric.electricity_total_MWh - electric.renewable_available_MWh)
    assert heating.iloc[0].start==baseline.snapshots[5]
    assert electric.iloc[0].start==baseline.snapshots[4]
    definition={'duration_hours':1,'fuel_link_disruptions':[{'fuel':'gas','reduction':1.,'locations':['DK0 0AC']}]}
    apply=Fields(input=Fields(net=str(tmp_path/'prepared.nc'),windows=str(tmp_path/'events.csv')),
                 output=Fields(net=str(tmp_path/'disrupted.nc'),report=str(tmp_path/'applied.csv')),
                 params=Fields(definition=definition,campaign='fuel',variant='2025',weather_year='2013-2014'),
                 wildcards=Fields(case='gas_100pct_1h'))
    timing_text=(tmp_path/'events.csv').read_text()
    (tmp_path/'events.csv').write_text(timing_text.replace(TIMING_METRICS['fuel'],'combined_electricity_heat_minus_wind_solar_v1'))
    with pytest.raises(ValueError,match='old timing metric'):
        runpy.run_path(str(root/'scripts/apply_disruption.py'),run_name='__main__',init_globals={'snakemake':apply})
    assert not (tmp_path/'disrupted.nc').exists()
    (tmp_path/'events.csv').write_text(timing_text)
    runpy.run_path(str(root/'scripts/apply_disruption.py'),run_name='__main__',init_globals={'snakemake':apply})
    report=pd.read_csv(tmp_path/'applied.csv')
    assert set(report.asset)=={'CCGT','boiler'}
    disrupted=solve_case('gas_100pct_1h',tmp_path/'disrupted.nc')
    selected=heating.iloc[0]
    base_metrics=metrics(baseline,'DK0 0AC',selected.start,selected.end)
    result=metrics(disrupted,'DK0 0AC',selected.start,selected.end)
    assert base_metrics['ens_power_MWh']==0
    assert result['ens_power_MWh']==2
    assert result['ens_heat_MWh']==5
    np.testing.assert_allclose(disrupted.links_t.p0.loc[disrupted.snapshots[5:6],'CCGT'],0,atol=1e-6)
    from scripts.plot_outage_dispatch import main, output_paths, DEFAULTS, zoom_windows
    assert list(zoom_windows(disrupted.snapshots,{'event':[selected.start,selected.end]}))==['event']
    plots=output_paths(tmp_path/'plots','DK2','gas_100pct_1h')
    main(tmp_path/'gas_100pct_1h/network_solved.nc',plots,DEFAULTS)
    assert all(Path(path).is_file() for path in plots.values())
    table=pd.DataFrame([{'variant':'2025','weather_year':'2013-2014','case':'gas_100pct_1h','period':'event',
                         'delta_ens_power_MWh':6.,'delta_ens_heat_MWh':1.}])
    outputs={f'2025_{kind}':str(tmp_path/f'heatmap_{kind}.png') for kind in ['power','heat']}
    plot_metrics(table,outputs)
    assert all(Path(path).is_file() for path in outputs.values())

    # The interconnector path reads the other row for the same duration.
    ic=Fields(input=apply.input,
              output=Fields(net=str(tmp_path/'interconnector.nc'),report=str(tmp_path/'interconnector.csv')),
              params=Fields(definition={'duration_hours':1,'interconnector_outages':[{'links':['cable']}]},
                            campaign='interconnector',variant='2025',weather_year='2013-2014'),
              wildcards=Fields(case='se_1h'))
    runpy.run_path(str(root/'scripts/apply_disruption.py'),run_name='__main__',init_globals={'snakemake':ic})
    ic_net=pypsa.Network(tmp_path/'interconnector.nc')
    assert pd.Timestamp(ic_net.meta['disruption_event']['start'])==baseline.snapshots[4]
    assert ic_net.links_t.p_max_pu.at[baseline.snapshots[4],'cable']==0
    assert disrupted.meta['disruption_event']['campaign']=='fuel'
    assert pd.Timestamp(disrupted.meta['disruption_event']['start'])==baseline.snapshots[5]

    # Summary evaluation also selects the fuel window, and rejects stale solved dates.
    import shutil
    from scripts.summarise_disruptions import campaign_metrics
    study=tmp_path/'study'
    base_dir=study/'base_cases/2025/2013-2014'
    case_dir=study/'fuel/2025/2013-2014/gas_100pct_1h'
    (base_dir/'base_weather').mkdir(parents=True)
    case_dir.mkdir(parents=True)
    for name in ['network_solved.nc','dispatch_summary.csv']:
        shutil.copy2(tmp_path/'base_weather'/name,base_dir/'base_weather'/name)
        shutil.copy2(tmp_path/'gas_100pct_1h'/name,case_dir/name)
    current_windows=windows.loc[windows.duration_hours.eq(1)].copy()
    current_windows.to_csv(base_dir/'event_windows.csv',index=False)
    summary_settings={'output_root':str(study),'campaign':'fuel','region':'DK0 0AC',
                      'variants':{'2025':{}},'weather_years':{'2013-2014':{}},
                      'fuels':['gas'],'reductions':[1.0],'durations_hours':[1]}
    table=campaign_metrics(summary_settings)
    assert pd.Timestamp(table.loc[table.period.eq('event'),'start'].iloc[0])==baseline.snapshots[5]
    mask=current_windows.campaign.eq('fuel')
    current_windows.loc[mask,'start']=baseline.snapshots[4]
    current_windows.loc[mask,'end']=baseline.snapshots[5]
    current_windows.to_csv(base_dir/'event_windows.csv',index=False)
    with pytest.raises(ValueError,match='obsolete/different event timing'):
        campaign_metrics(summary_settings)
