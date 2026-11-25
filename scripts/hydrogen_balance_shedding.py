# SPDX-License-Identifier: MIT
"""Priced missing hydrogen for process consumption, including buses without Loads."""
from collections.abc import Mapping
from numbers import Real
import re

import numpy as np
import pandas as pd
import xarray as xr

META_KEY='hydrogen_balance_shedding'
CARRIER='load-shedding-hydrogen-balance'
BUS_COLUMNS=['generator','bus','region','country','penalty_EUR_per_MWh',
             'missing_hydrogen_MWh','peak_missing_hydrogen_MW','shedding_hours',
             'process_hydrogen_use_MWh','penalty_cost_EUR']


def positive_number(value,label):
    if isinstance(value,bool) or not isinstance(value,Real) or not np.isfinite(value) or value<=0:
        raise ValueError(f'{label} must be positive and finite.')
    return float(value)


def process_terms(n,bus):
    """Positive H2 withdrawals from forward, non-storage/transport Link flows."""
    terms=[]
    h2_buses=set(n.buses.index[n.buses.carrier.isin(['H2','H2 Store'])])
    for port in [c for c in n.links if re.fullmatch(r'bus\d+',c)]:
        index=int(port[3:])
        for name in n.links.index[n.links[port].eq(bus)]:
            row=n.links.loc[name]
            # Transferring H2 between H2/storage buses is not consumption.
            endpoints=[row[c] for c in n.links if re.fullmatch(r'bus\d+',c) and row[c]]
            if endpoints and all(endpoint in h2_buses for endpoint in endpoints):
                continue
            coefficient=(pd.Series(1.0,index=n.snapshots) if index==0 else
                         -n.get_switchable_as_dense('Link','efficiency' if index==1 else f'efficiency{index}')[name])
            if not np.isfinite(coefficient.to_numpy()).all():
                raise ValueError(f'Nonfinite H2 consumption coefficient on {name}.')
            if not coefficient.gt(0).any():
                continue
            if n.get_switchable_as_dense('Link','p_min_pu')[name].lt(0).any():
                raise ValueError(f'Hydrogen process shedding requires forward-only consumption Links: {name}')
            terms.append({'link':name,'port':index})
    return terms


def coefficient(n,term,snapshots):
    port=term['port']
    if port==0:
        return pd.Series(1.0,index=snapshots)
    attr='efficiency' if port==1 else f'efficiency{port}'
    return -n.get_switchable_as_dense('Link',attr,snapshots)[term['link']].clip(upper=0)


def add_hydrogen_balance_shedding(n,fuel_options):
    settings=fuel_options.get('hydrogen_balance',{})
    if not isinstance(settings,Mapping):
        raise ValueError('fuel.hydrogen_balance must be a mapping.')
    if set(settings)-{'enable','cost','costs_by_bus'}:
        raise ValueError('Unknown hydrogen_balance settings.')
    if not isinstance(settings.get('enable',False),bool):
        raise ValueError('hydrogen_balance.enable must be boolean.')
    old=n.generators.index[n.generators.carrier.eq(CARRIER)]
    if not fuel_options.get('enable',False) or not settings.get('enable',False):
        n.remove('Generator',old)
        n.meta={**n.meta,META_KEY:{}}
        return
    override=settings.get('cost')
    if override is not None:
        positive_number(override,'hydrogen_balance.cost')
    bus_costs=settings.get('costs_by_bus',{})
    if not isinstance(bus_costs,Mapping):
        raise ValueError('hydrogen_balance.costs_by_bus must be a mapping.')
    for bus,cost in bus_costs.items():
        if bus not in n.buses.index or n.buses.at[bus,'carrier']!='H2':
            raise ValueError(f'Unknown H2 bus price override: {bus}')
        positive_number(cost,f'Hydrogen cost for {bus}')
    records,profiles={},{}
    upper=n.get_switchable_as_dense('Link','p_max_pu')
    for bus in n.buses.index[n.buses.carrier.eq('H2')]:
        terms=process_terms(n,bus)
        if not terms:
            continue
        envelope=pd.Series(0.0,index=n.snapshots)
        for term in terms:
            link=term['link']
            if n.links.at[link,'p_nom_extendable']:
                raise ValueError('Freeze capacities before adding H2 process shedding.')
            rating=float(n.links.at[link,'p_nom'])
            if not np.isfinite(rating) or rating<0:
                raise ValueError(f'Invalid hydrogen-consuming capacity: {link}')
            envelope+=coefficient(n,term,n.snapshots)*upper[link].clip(lower=0)*rating
        if not np.isfinite(envelope.to_numpy()).all():
            raise ValueError(f'Nonfinite H2 process requirement at {bus}.')
        peak=float(envelope.max())
        if peak<=0:
            continue
        row=n.buses.loc[bus]
        region=row.get('location','') or row.get('country','') or bus
        inherited=fuel_options.get('costs_by_carrier',{}).get('H2',
            fuel_options.get('costs_by_fuel',{}).get('hydrogen',fuel_options.get('default_cost',23300)))
        cost=bus_costs.get(bus,override if override is not None else inherited)
        cost+=fuel_options.get('regional_cost_adders',{}).get(region,0)
        cost=positive_number(cost,f'Hydrogen balance price for {bus}')
        name=bus+' hydrogen balance shedding'
        if name in n.generators.index and name not in old:
            raise ValueError(f'Hydrogen shedding name collision: {name}')
        records[name]={'bus':bus,'region':region,'country':row.get('country',''),
                       'penalty_EUR_per_MWh':cost,'capacity_MW':peak,'terms':terms}
        profiles[name]=envelope/peak
    n.remove('Generator',old)
    if records and CARRIER not in n.carriers.index:
        n.add('Carrier',CARRIER,nice_name='Missing hydrogen supply for processes')
    for name,row in records.items():
        n.add('Generator',name,bus=row['bus'],carrier=CARRIER,p_nom=row['capacity_MW'],
              p_nom_extendable=False,p_min_pu=0,p_max_pu=profiles[name],marginal_cost=row['penalty_EUR_per_MWh'])
    n.meta={**n.meta,META_KEY:records}


def add_hydrogen_shedding_constraints(n,snapshots):
    """Missing H2 cannot exceed actual process consumption or fund pure stock filling."""
    records=n.meta.get(META_KEY,{})
    for i,(name,row) in enumerate(records.items()):
        consumption=0
        for term in row['terms']:
            values=xr.DataArray(coefficient(n,term,snapshots).to_numpy(),dims=['snapshot'],
                                coords={'snapshot':snapshots})
            consumption+=values*n.model['Link-p'].sel(snapshot=snapshots,name=term['link'],drop=True)
        missing=n.model['Generator-p'].sel(snapshot=snapshots,name=name,drop=True)
        n.model.add_constraints(missing<=consumption,name=f'Hydrogen-process-shedding-bound-{i}')


def hydrogen_shedding_reports(n):
    records=n.meta.get(META_KEY,{})
    names=list(records)
    power=n.generators_t.p.reindex(index=n.snapshots,columns=names,fill_value=0).clip(lower=0)
    if not np.isfinite(power.to_numpy()).all():
        raise ValueError('Missing/nonfinite hydrogen shedding dispatch.')
    costs=n.get_switchable_as_dense('Generator','marginal_cost')
    weights=n.snapshot_weightings.generators
    rows=[]
    for name,row in records.items():
        consumption=pd.Series(0.0,index=n.snapshots)
        for term in row['terms']:
            flow=n.links_t.p0.reindex(index=n.snapshots,columns=[term['link']],fill_value=0)[term['link']]
            consumption+=flow.clip(lower=0)*coefficient(n,term,n.snapshots)
        if (power[name]-consumption>1e-4).any():
            raise ValueError(f'Hydrogen shedding exceeds process consumption at {row["bus"]}')
        rows.append({'generator':name,**{k:v for k,v in row.items() if k not in ('terms','capacity_MW')},
            'missing_hydrogen_MWh':float(power[name].mul(weights).sum()),
            'peak_missing_hydrogen_MW':float(power[name].max()),
            'shedding_hours':float(weights.loc[power[name]>1e-6].sum()),
            'process_hydrogen_use_MWh':float(consumption.mul(weights).sum()),
            'penalty_cost_EUR':float((power[name]*costs[name]*n.snapshot_weightings.objective).sum())})
    return {'hydrogen_shedding_by_bus':pd.DataFrame(rows,columns=BUS_COLUMNS),
            'hydrogen_shedding_timeseries':power.rename_axis('snapshot').reset_index()}
