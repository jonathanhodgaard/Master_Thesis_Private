import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.analyse_disruption_windows import (
    select_windows, residual_load, campaign_windows, select_campaign_windows, TIMING_METRICS,
)
from scripts.apply_disruption import apply_scenario
from scripts.fuel_link_disruption import apply_fuel_link_disruptions


def test_windows_match_brute_force_signed_energy_and_first_tie():
    index = pd.date_range('2013-07-01', periods=2200, freq='h')
    values = pd.Series(np.random.default_rng(42).normal(0, 20, len(index)), index=index)
    durations = [1,24,168,720,2160]
    selected = select_windows(values, durations)
    for row in selected.itertuples():
        sums = [values.iloc[i:i+row.duration_hours].sum() for i in range(len(values)-row.duration_hours+1)]
        position = int(np.argmax(sums))
        assert row.start == index[position]
        assert row.end-row.start == pd.Timedelta(hours=row.duration_hours)
        assert row.residual_energy_MWh == pytest.approx(max(sums))
    tie = select_windows(pd.Series([1.,1.,1.], index=index[:3]), [2]).iloc[0]
    assert tie.start == index[0]
    negative = select_windows(pd.Series([15.,-100.,6.,6.], index=index[:4]), [2]).iloc[0]
    assert negative.start == index[2]  # Clipping negative residuals would change the selection.


@pytest.mark.parametrize('durations', [[0],[True],[1,1],[5]])
def test_invalid_durations_rejected(durations):
    with pytest.raises(ValueError):
        select_windows(pd.Series([1.,2.,3.],index=pd.date_range('2013-01-01',periods=3,freq='h')),durations)


def test_residual_uses_available_renewables_and_excludes_transfer_double_counting():
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2013-07-01',periods=3,freq='h'))
    for name,carrier,location in [('DK0 0AC','AC','DK0 0AC'),('lv','low voltage','DK0 0AC'),
            ('heat','urban central heat','DK0 0AC'),('battery','battery','DK0 0AC'),
            ('foreign','AC','SE0 0AC')]:
        n.add('Bus',name,carrier=carrier,location=location)
    n.add('Load','fixed',bus='lv',p_set=10)
    n.add('Load','useful heat',bus='heat',p_set=6)
    n.add('Link','distribution',bus0='DK0 0AC',bus1='lv',p_nom=50)
    n.add('Link','import',bus0='foreign',bus1='DK0 0AC',p_nom=50)
    n.add('Link','hp',bus0='lv',bus1='heat',p_nom=10)
    n.add('Link','charger',bus0='DK0 0AC',bus1='battery',p_nom=10)
    n.links_t.p0=pd.DataFrame({'distribution':[10]*3,'import':[8]*3,'hp':[2]*3,'charger':[4]*3},index=n.snapshots)
    n.add('Generator','solar',bus='lv',carrier='solar rooftop',p_nom=10,p_max_pu=.6)
    n.generators_t.p=pd.DataFrame({'solar':[1]*3},index=n.snapshots)
    n._objective=0.0  # Synthetic solved dispatch above.
    n.add('Link','idle charger',bus0='DK0 0AC',bus1='battery',p_nom=0)
    n.add('StorageUnit','idle storage',bus='DK0 0AC',p_nom=0)
    series=residual_load(n,'DK0 0AC',['solar rooftop'])
    np.testing.assert_allclose(series.electricity_total_MW,16)
    np.testing.assert_allclose(series.electricity_to_heat_MW,2)
    np.testing.assert_allclose(series.electricity_excluding_heat_MW,14)
    np.testing.assert_allclose(series.heat_demand_MW,6)
    np.testing.assert_allclose(series.renewable_available_MW,6)
    np.testing.assert_allclose(series.electrical_residual_load_MW,10)
    np.testing.assert_allclose(series.heat_stress_MW,0)


@pytest.mark.parametrize('fuel',['gas','oil','biomass'])
@pytest.mark.parametrize('reduction',[.33,.66,1.0])
def test_dk2_only_reduction_preserves_infrastructure_and_other_regions(fuel,reduction):
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2013-07-01',periods=6,freq='h'))
    raw='solid biomass' if fuel=='biomass' else fuel
    n.add('Bus','EU fuel',carrier=raw,location='EU')
    n.add('Bus','other raw',carrier=raw,location='DK0 0AC')
    for region in ['DK0 0AC','DK2 0AC','SE0 0AC']:
        n.add('Bus',region,carrier='AC',location=region)
        n.add('Link',region+' plant',bus0='EU fuel',bus1=region,p_nom=10,p_max_pu=.8,efficiency=.5)
    n.add('Link','transport',bus0='EU fuel',bus1='other raw',p_nom=100)
    before=n.links.copy(deep=True)
    event={'name':'test','fuel':fuel,'reduction':reduction,'locations':['DK0 0AC'],
           'start':str(n.snapshots[2]),'end':str(n.snapshots[4])}
    apply_fuel_link_disruptions(n,[event])
    pmax=n.get_switchable_as_dense('Link','p_max_pu')
    np.testing.assert_allclose(pmax.loc[n.snapshots[2:4],'DK0 0AC plant'],.8*(1-reduction))
    np.testing.assert_allclose(pmax.loc[n.snapshots[[0,1,4,5]],'DK0 0AC plant'],.8)
    np.testing.assert_allclose(pmax[['DK2 0AC plant','SE0 0AC plant']],.8)
    np.testing.assert_allclose(pmax['transport'],1)
    pd.testing.assert_frame_equal(n.links,before)
    assert len(n.meta['fuel_link_disruptions'])==1
    windows=pd.DataFrame([{'campaign':'fuel','timing_metric':TIMING_METRICS['fuel'],
                           'duration_hours':2,'start':n.snapshots[0],'end':n.snapshots[3]}])
    with pytest.raises(ValueError,match='duration'):
        apply_scenario(n,{'duration_hours':2},windows,'bad','fuel')


def test_one_hour_zoom_has_visible_hourly_area():
    import matplotlib.pyplot as plt
    from scripts.plot_outage_dispatch import plot_zoom

    start = pd.Timestamp('2013-07-01 04:00')
    end = start + pd.Timedelta(hours=1)
    balance = pd.DataFrame({'Load shedding':[6.], 'Demand':[-6.]}, index=pd.DatetimeIndex([start]))
    figure = plot_zoom(balance, 'DK2', 'power', {}, {'Event':(str(start),str(end))})
    for collection in figure.axes[0].collections:
        vertices = collection.get_paths()[0].vertices
        assert np.ptp(vertices[:,0]) == pytest.approx(1/24)
    plt.close(figure)


def test_timing_rejects_an_unsolved_network():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range('2013-07-01',periods=2,freq='h'))
    with pytest.raises(ValueError,match='successfully solved'):
        residual_load(n,'DK0 0AC',['solar'])


def test_split_metrics_include_heating_electricity_and_count_final_heat_once():
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2013-07-01',periods=3,freq='h'))
    for name,carrier,location in [
            ('DK0 0AC','AC','DK0 0AC'),('heat','urban central heat','DK0 0AC'),
            ('rural','residential rural heat','DK0 0AC'),
            ('household','residential rural heat','DK0 0AC'),
            ('tank','urban central water pits','DK0 0AC'),('H2','H2','DK0 0AC'),
            ('source heat','geothermal heat','DK0 0AC'),
            ('foreign heat','urban central heat','SE0 0AC')]:
        n.add('Bus',name,carrier=carrier,location=location)
    n.add('Load','electricity',bus='DK0 0AC',p_set=10)
    n.add('Load','district heat',bus='heat',p_set=[6,12,18])
    n.add('Load','household heat',bus='rural',p_set=3)
    n.add('Load','foreign heat',bus='foreign heat',p_set=100)
    n.add('Link','forward pump',bus0='DK0 0AC',bus1='heat',carrier='air heat pump',p_nom=20,efficiency=3)
    n.add('Link','reverse pump',bus0='heat',bus1='DK0 0AC',carrier='heat pump',p_nom=20,
          efficiency=1/3,p_min_pu=-3,p_max_pu=0)
    n.add('Link','tank heater',bus0='DK0 0AC',bus1='tank',carrier='resistive heater',p_nom=10)
    n.add('Link','household delivery',bus0='household',bus1='rural',carrier='household heat delivery',p_nom=10)
    # H2 production with byproduct heat is not dedicated electric heating.
    n.add('Link','electrolysis',bus0='DK0 0AC',bus1='H2',bus2='heat',carrier='H2 electrolysis',p_nom=10)
    n.add('Link','direct heat',bus0='source heat',bus1='heat',bus2='DK0 0AC',
          carrier='heat direct utilisation',p_nom=10,efficiency2=-.5)
    n.links_t.p2=pd.DataFrame({'direct heat':[.5,.5,.5]},index=n.snapshots)
    n.links_t.p0=pd.DataFrame({'forward pump':[2,3,4],'reverse pump':[-3,-6,-9],
                              'tank heater':[1,1,1],'household delivery':[3,3,3],
                              'electrolysis':[5,5,5]},index=n.snapshots)
    n.links_t.p1=pd.DataFrame({'reverse pump':[1,2,3]},index=n.snapshots)
    n.add('Store','thermal stock',bus='tank',e_nom=100)
    n.stores_t.p=pd.DataFrame({'thermal stock':[-20,30,-10]},index=n.snapshots)
    n.add('Generator','unmet heat',bus='heat',carrier='load-shedding-heat',p_nom=10)
    n.add('Generator','solar',bus='DK0 0AC',carrier='solar',p_nom=10,p_max_pu=.6)
    n._objective=0.
    result=residual_load(n,'DK0 0AC',['solar'])
    np.testing.assert_allclose(result.electricity_total_MW,[19.5,21.5,23.5])
    np.testing.assert_allclose(result.electricity_to_heat_MW,[4.5,6.5,8.5])
    np.testing.assert_allclose(result.electricity_excluding_heat_MW,15)
    np.testing.assert_allclose(result.heat_demand_MW,[9,15,21])
    np.testing.assert_allclose(result.electrical_residual_load_MW,[13.5,15.5,17.5])
    np.testing.assert_allclose(result.heat_stress_MW,[3,9,15])


def test_heat_and_electricity_select_different_campaign_hours():
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2013-07-01',periods=3,freq='h'))
    n.add('Bus','DK0 0AC',carrier='AC',location='DK0 0AC')
    n.add('Bus','heat',carrier='urban central heat',location='DK0 0AC')
    n.add('Load','electricity',bus='DK0 0AC',p_set=[10,5,5])
    n.add('Load','heat',bus='heat',p_set=[0,20,0])
    n.add('Generator','wind',bus='DK0 0AC',carrier='onwind',p_nom=1,p_max_pu=0)
    n._objective=0.
    hourly=residual_load(n,'DK0 0AC',['onwind'])
    windows=campaign_windows(hourly,[1,2])
    electrical=select_campaign_windows(windows,"interconnector",[1,2])
    heating=select_campaign_windows(windows,"fuel",[1,2])
    assert electrical.loc[electrical.duration_hours.eq(1)].iloc[0].start==n.snapshots[0]
    assert heating.loc[heating.duration_hours.eq(1)].iloc[0].start==n.snapshots[1]
    assert len(windows)==4
    assert set(windows.timing_metric)==set(TIMING_METRICS.values())


@pytest.mark.parametrize('campaign',['interconnector','fuel'])
def test_old_combined_and_wrong_family_tables_rejected(campaign):
    old=pd.DataFrame([{'duration_hours':1,'timing_metric':'combined_electricity_heat_minus_wind_solar_v1'}])
    with pytest.raises(ValueError,match='Old timing metric'):
        select_campaign_windows(old,campaign,[1])
    other='fuel' if campaign=='interconnector' else 'interconnector'
    wrong=pd.DataFrame([{'campaign':other,'duration_hours':1,'timing_metric':TIMING_METRICS[other]}])
    with pytest.raises(ValueError,match='timing metric'):
        select_campaign_windows(wrong,campaign,[1])
    correct=pd.DataFrame([{'campaign':campaign,'duration_hours':1,'timing_metric':TIMING_METRICS[campaign]}])
    with pytest.raises(ValueError,match='Duplicate'):
        select_campaign_windows(pd.concat([correct,correct]),campaign)
    with pytest.raises(ValueError,match='durations differ'):
        select_campaign_windows(correct,campaign,[1,24])
