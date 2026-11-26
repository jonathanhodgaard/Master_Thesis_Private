import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.analyse_disruption_windows import select_windows, residual_load
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
    np.testing.assert_allclose(series.load_MW,16)
    np.testing.assert_allclose(series.renewable_available_MW,6)
    np.testing.assert_allclose(series.residual_load_MW,10)


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
    windows=pd.DataFrame([{'duration_hours':2,'start':n.snapshots[0],'end':n.snapshots[3]}])
    with pytest.raises(ValueError,match='duration'):
        apply_scenario(n,{'duration_hours':2},windows,'bad')


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
