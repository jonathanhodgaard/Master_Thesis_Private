from pathlib import Path
import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.plot_outage_dispatch import DEFAULTS, main, output_paths, shift_years, weather_year_offset


@pytest.mark.parametrize('start,folder,expected',[
    ('2013-07-01','1965-1966',-48),
    ('2014-01-01','1965-1966',-48),
    ('2014-01-01','2013-2014',0),
    ('2014-01-01','2019-2020',6),
    ('2014-06-01','1968-1969',-45),
    ('2014-01-01','other',0),
])
def test_weather_labels_use_july_june_year_for_full_and_winter_runs(start,folder,expected):
    index=pd.date_range(start,periods=2,freq='h')
    offset=weather_year_offset(Path('results')/folder/'gas_reduction_50/network_solved.nc',index)
    assert offset==expected
    shifted=shift_years(pd.DataFrame({'power':[1,2]},index=index),offset)
    assert shifted.index[0].year==index[0].year+expected


def test_all_eight_dispatch_figures_render_from_a_winter_only_saved_network(tmp_path):
    n=pypsa.Network()
    n.set_snapshots(pd.date_range('2014-01-01',periods=28*24,freq='h'))
    region='DK0 0AC'
    n.add('Bus',region,carrier='AC',location=region)
    n.add('Bus',region+' heat',carrier='rural heat',location=region)
    n.add('Generator','wind',bus=region,carrier='onwind',p_nom=10)
    n.add('Generator','boiler',bus=region+' heat',carrier='rural gas boiler',p_nom=10)
    n.add('Load','electricity',bus=region,p_set=5)
    n.add('Load','heat',bus=region+' heat',p_set=2)
    n.generators_t.p=pd.DataFrame({'wind':5.0,'boiler':2.0},index=n.snapshots)
    n.loads_t.p=pd.DataFrame({'electricity':5.0,'heat':2.0},index=n.snapshots)
    path=tmp_path/'1965-1966'/'gas_reduction_50'/'network_solved.nc'
    path.parent.mkdir(parents=True)
    n.export_to_netcdf(path)
    outputs=output_paths(path.parent/'plots','DK2','gas_reduction_50_1965-1966')
    main(path,outputs,dict(DEFAULTS))
    assert len(outputs)==8
    for output in outputs.values():
        assert output.exists() and output.stat().st_size>1000
