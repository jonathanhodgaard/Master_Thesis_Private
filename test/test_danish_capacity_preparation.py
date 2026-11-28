"""Reproduce prepared Danish inputs from the shipped sources, without a sibling repo."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import yaml

from scripts import prepare_danish_power_capacities as power
from scripts import prepare_danish_heat_capacities as heat
from scripts import prepare_danish_rural_heat as rural
from scripts.lib.validation.config.electricity import ElectricityConfig

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/danish_capacity_sources'
PREPARED = ROOT / 'data/existing_infrastructure'


def test_shipped_source_snapshots_match_manifest():
    manifest=json.loads((RAW/'manifest.json').read_text(encoding='utf-8'))
    assert len(manifest['files'])==5
    for name,record in manifest['files'].items():
        path=RAW/name
        assert path.stat().st_size==record['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256']


@pytest.fixture(scope='module')
def regenerated(tmp_path_factory):
    output=tmp_path_factory.mktemp('prepared_danish_inputs')
    before={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in RAW.iterdir() if path.is_file()}
    with patch.object(power,'postcode_to_coords',side_effect=AssertionError('Normal preparation must stay offline')):
        power.main(RAW,output)
    heat.main(RAW,output)
    rural.main(RAW,output)
    after={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in RAW.iterdir() if path.is_file()}
    assert before==after
    return output


@pytest.mark.parametrize('name',[
    'powerplants.csv','existing_chp_heat_capacities.csv',
    'existing_rural_decentral_heat_capacities.csv','existing_solar_rooftop_capacities.csv'])
def test_prepared_inputs_reproduce_checked_in_tables(regenerated,name):
    expected=pd.read_csv(PREPARED/name,low_memory=False)
    actual=pd.read_csv(regenerated/name,low_memory=False)
    pd.testing.assert_frame_equal(actual,expected,check_dtype=False)
    if 'node' in actual:
        assert set(actual.node)=={'DK1','DK2'}


def test_fleet_boundary_and_pv_split(regenerated):
    fleet=pd.read_csv(regenerated/'powerplants.csv',low_memory=False)
    assert fleet.id.is_unique
    dk=fleet.loc[fleet.Country.eq('Denmark')]
    assert dk.Set.eq('PP').all() # CHP is supplied through its separate capacity table.
    assert dk.loc[dk.Fueltype.eq('Solar'),'Capacity'].gt(power.PV_CAPACITY_THRESHOLD_KW/1000).all()
    assert len(dk.loc[dk.Fueltype.eq('Solar')])>0
    others=fleet.loc[~fleet.Country.eq('Denmark')]
    assert (others.DateIn.isna() | others.DateIn.le(2025)).all()
    assert (others.DateOut.isna() | others.DateOut.gt(2025)).all()
    rooftop=pd.read_csv(regenerated/'existing_solar_rooftop_capacities.csv')
    assert rooftop.capacity.sum()==pytest.approx(1799.51861)


def test_cache_is_read_only_and_unknown_sentinel_does_not_query(tmp_path):
    cache=tmp_path/'postcode_coordinates.csv'
    pd.DataFrame([{'postcode':'4000','longitude':12.,'latitude':55.}]).to_csv(cache,index=False)
    before=cache.read_bytes()
    with patch.object(power,'postcode_to_coords',side_effect=AssertionError('No implicit lookups')):
        result=power.load_postcode_coords(['4000','9999'],cache)
        assert result['4000']['longitude']==12
        assert np.isnan(result['9999']['latitude'])
        with pytest.raises(ValueError,match='explicitly refresh'):
            power.load_postcode_coords(['5000'],cache)
    assert cache.read_bytes()==before
    with patch.object(power,'postcode_to_coords',return_value={'longitude':10.4,'latitude':55.4}) as lookup:
        power.load_postcode_coords(['5000'],cache,refresh=True)
        lookup.assert_called_once_with('5000')
    assert power.load_postcode_coords(['5000'],cache)['5000']['longitude']==10.4


@pytest.mark.parametrize('postcode,zone',[('4000','DK2'),('4999','DK2'),('5000','DK1'),('8000','DK1')])
def test_geographical_aliases_match_in_all_preparations(postcode,zone):
    assert power.postcode_to_node(postcode)==zone
    assert heat.postcode_to_zone(postcode)==zone


def test_prepared_input_config_is_optional_and_selected_by_thesis_variants():
    assert ElectricityConfig().prepared_powerplants is None
    assert ElectricityConfig(prepared_powerplants='prepared.csv').prepared_powerplants=='prepared.csv'
    for variant in ['2025','2035_wo_V2G','2035_w_V2G']:
        config=yaml.safe_load((ROOT/f'config/config.sector_{variant}.yaml').read_text(encoding='utf-8'))
        assert config['electricity']['prepared_powerplants']=='data/existing_infrastructure/powerplants.csv'
    schema=json.loads((ROOT/'config/schema.default.json').read_text(encoding='utf-8'))
    assert 'prepared_powerplants' in schema['properties']['electricity']['properties']


def test_real_ppm_reader_accepts_prepared_fleet(regenerated):
    import powerplantmatching as pm
    table=(pd.read_csv(regenerated/'powerplants.csv',index_col=0)
           .pipe(pm.collection.parse_string_to_dict,['projectID','EIC'])
           .pipe(pm.collection.set_column_name,'Matched Data'))
    table=table.powerplant.convert_country_to_alpha2()
    assert table.Country.eq('DK').sum()==5879
