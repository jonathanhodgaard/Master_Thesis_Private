import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.disruption_diagnostics import RollingDiagnostics
from scripts.solve_disruption_dispatch import write_infeasibility_report


def test_state_saved_only_on_failure_and_retry_preserves_failure(tmp_path):
    stores=pd.DataFrame([{'carrier':'gas','bus':'gas','e_initial':25.}],index=['tank'])
    units=pd.DataFrame(columns=['carrier','bus','state_of_charge_initial'])
    n=SimpleNamespace(components={'Store':SimpleNamespace(static=stores),'StorageUnit':SimpleNamespace(static=units)})
    hours=pd.date_range('2013-07-01',periods=4,freq='h')
    d=RollingDiagnostics(tmp_path,{'reference':'base.nc'})
    assert not (tmp_path/'failures').exists()
    with d.window(n,0,hours,hours[:2],[]):
        d.committed({'window':0})
    assert not (tmp_path/'storage_opening.csv').exists()
    with pytest.raises(ValueError,match='test failure'):
        with d.window(n,1,hours,hours[:2],[]):
            (tmp_path/'solver.log').write_text('failed solver evidence')
            d.phase('solve_failed',solver_status='warning',termination_condition='infeasible')
            raise ValueError('test failure')
    status=json.loads((tmp_path/'status.json').read_text())
    assert status['window']==1 and status['last_completed_window']==0
    failure=d.failure_directory
    assert pd.read_csv(failure/'storage_opening.csv').opening_MWh.tolist()==[25]
    assert (failure/'solver.log').read_text()=='failed solver evidence'
    assert 'ValueError: test failure' in (failure/'failure.txt').read_text()
    retry=RollingDiagnostics(tmp_path,{})
    assert retry.current['phase']=='initializing'
    assert failure.exists()
    assert not (tmp_path/'window_summary.csv').exists()


def test_infeasibility_report_is_compact_and_model_export_opt_in(tmp_path):
    written=[]
    class Solver:
        IISMinimal=False
        def setParam(self,name,value):
            assert name=='TimeLimit' and value==10
        def write(self,path):
            written.append(path)
            Path(path).write_text('Bounds: named-variable >= 4')
    model=SimpleNamespace(solver_model=Solver(),compute_infeasibilities=lambda:[4,9],
                          constraints=SimpleNamespace(print_labels=lambda labels: print('Bus balance conflict',labels)))
    path=tmp_path/'infeasibility.txt'
    write_infeasibility_report(SimpleNamespace(model=model),path,10)
    assert 'Bus balance conflict [4, 9]' in path.read_text()
    assert 'named-variable' in path.read_text()
    assert [p.name for p in tmp_path.iterdir()]==['infeasibility.txt']
    write_infeasibility_report(SimpleNamespace(model=model),path,10,True)
    assert path.with_suffix('.mps.gz').exists()
    model.compute_infeasibilities=lambda: (_ for _ in ()).throw(RuntimeError('unavailable'))
    write_infeasibility_report(SimpleNamespace(model=model),path,10)
    assert 'unavailable' in path.read_text()
