import pandas as pd
import pypsa
import pytest

from scripts.prepare_network_disruptions import restore_original_capacities, set_biomass_import_capacity


def test_original_capacity_restored_and_only_biomass_import_resized():
    n=pypsa.Network()
    n.add('Bus','import',carrier='solid biomass import')
    n.add('Bus','fuel',carrier='solid biomass')
    n.add('Bus','grid',carrier='AC')
    n.add('Bus','lv',carrier='low voltage')
    n.add('Link','import delivery',bus0='import',bus1='fuel',carrier='solid biomass import',p_nom=3)
    n.add('Link','distribution',bus0='grid',bus1='lv',p_nom=12)
    n.add('Link','delivery',bus0='import',bus1='fuel',p_nom=20)
    n.meta={'low_voltage_capacity_adjustments':[{'link':'distribution','original_input_capacity_MW':10}],
            'fuel_delivery_capacity_adjustments':{'delivery':{'original_capacities':{'p_nom':10}}}}
    restore_original_capacities(n)
    assert n.links.at['distribution','p_nom']==10
    assert n.links.at['delivery','p_nom']==10
    before=n.links.loc[['distribution','delivery']].copy(deep=True)
    set_biomass_import_capacity(n,50000)
    assert n.links.at['import delivery','p_nom']==50000
    assert not n.links.at['import delivery','p_nom_extendable']
    pd.testing.assert_frame_equal(before,n.links.loc[['distribution','delivery']])
    restore_original_capacities(n) # Already clean: no further changes.
    with pytest.raises(ValueError):
        set_biomass_import_capacity(n,float('nan'))
