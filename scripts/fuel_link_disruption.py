# SPDX-License-Identifier: MIT
"""Dated reductions at a single fuel-consumption boundary."""
from collections.abc import Mapping
from numbers import Real

import numpy as np
import pandas as pd


META_KEY = 'fuel_link_disruptions'
REPORT_COLUMNS = ['event', 'fuel', 'link', 'bus0', 'bus1', 'country', 'start', 'end',
                  'hours', 'reduction', 'remaining_fraction', 'p_nom_MW',
                  'normal_min_capacity_MW', 'normal_max_capacity_MW',
                  'restricted_min_capacity_MW', 'restricted_max_capacity_MW']


def consuming_links(n, fuel):
    """One fuel-use boundary: raw fuel bus -> conversion equipment/end-use bus.

    Pipelines and transport joining two buses of the same raw fuel are excluded.
    Oil refining is upstream (oil primary -> oil) and is not a final-oil use.
    """
    if fuel not in ('gas', 'oil', 'biomass'):
        raise ValueError("Fuel-use disruptions support gas, oil or biomass.")
    input_fuel = n.links.bus0.map(n.buses.carrier)
    output_fuel = n.links.bus1.map(n.buses.carrier)
    carriers = ['biomass', 'solid biomass'] if fuel == 'biomass' else [fuel]
    mask = input_fuel.isin(carriers) & ~output_fuel.isin(carriers)
    mask &= ~n.links.carrier.isin(['gas pipeline', 'gas pipeline new',
                                  'oil pipeline', 'oil refining'])
    return n.links.index[mask]


def string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f'{label} must be a list of nonempty strings.')
    if len(value) != len(set(value)):
        raise ValueError(f'{label} contains duplicate names.')
    return value


def link_country(n, names):
    buses = n.buses.loc[n.links.loc[names, 'bus1']]
    # Attribute each use to its consuming bus, including uses of shared EU oil.
    country = buses.get('country', pd.Series('', index=buses.index)).fillna('')
    location = buses.get('location', pd.Series('', index=buses.index)).fillna('')
    fallback = location.where(location.ne(''), buses.index.to_series()).str[:2]
    return pd.Series(country.where(country.ne(''), fallback).to_numpy(), index=names)


def apply_fuel_link_disruptions(n, events):
    """Reduce consuming Links once, after weather/capacity preparation.

    Select gas/oil/biomass uses, exact eligible Links, and/or destination countries.
    Do not restrict upstream supply, pipelines or refining for the same fuel.
    """
    if not isinstance(events, list):
        raise ValueError('fuel_link_disruptions must be a list.')
    if not events:
        return
    if n.meta.get(META_KEY):
        raise ValueError('Fuel-use disruptions already applied; start from a fresh prepared network.')
    snapshots = n.snapshots
    if (not isinstance(snapshots, pd.DatetimeIndex) or snapshots.empty
            or snapshots.tz is not None or not snapshots.is_unique
            or not snapshots.is_monotonic_increasing):
        raise ValueError('Fuel disruptions require unique increasing timezone-naive datetime snapshots.')
    if len(snapshots) > 1 and not ((snapshots[1:] - snapshots[:-1]) == pd.Timedelta(hours=1)).all():
        raise ValueError('Fuel disruptions require consecutive hourly snapshots.')
    end_of_network = snapshots[-1] + pd.Timedelta(hours=1)
    normal = n.get_switchable_as_dense('Link', 'p_max_pu')
    minimum = n.get_switchable_as_dense('Link', 'p_min_pu')
    profile = n.links_t.p_max_pu.reindex(snapshots).copy()
    occupied, records, event_names = {}, [], set()
    for i, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise ValueError('Each fuel disruption must be a mapping.')
        unknown = set(event) - {'name', 'fuel', 'reduction', 'start', 'end', 'links', 'countries', 'locations'}
        if unknown:
            raise ValueError(f'Unknown fuel disruption settings: {sorted(unknown)}')
        fuel = event.get('fuel')
        names = consuming_links(n, fuel)
        name = event.get('name', f'{fuel}_use_{i}')
        if not isinstance(name, str) or not name or name in event_names:
            raise ValueError('Fuel disruption names must be unique nonempty strings.')
        event_names.add(name)
        reduction = event.get('reduction')
        if (isinstance(reduction, bool) or not isinstance(reduction, Real)
                or not np.isfinite(reduction) or not 0 <= reduction <= 1):
            raise ValueError('reduction must be a finite fraction between 0 and 1.')
        if event.get('start') is None or event.get('end') is None:
            raise ValueError('Fuel disruptions require explicit start and end dates.')
        start, end = pd.Timestamp(event['start']), pd.Timestamp(event['end'])
        if (pd.isna(start) or pd.isna(end) or start.tzinfo is not None or end.tzinfo is not None
                or not start < end or start not in snapshots
                or (end not in snapshots and end != end_of_network)):
            raise ValueError('Fuel disruption bounds must align with the network calendar and start before end.')
        if 'links' in event:
            explicit = pd.Index(string_list(event['links'], 'links'))
            invalid = explicit.difference(names)
            if len(invalid):
                raise ValueError(f'Selected names are not {fuel} consuming Links: {invalid.tolist()}')
            names = explicit
        countries = link_country(n, names)
        if 'countries' in event:
            selected = string_list(event['countries'], 'countries')
            names = names[countries.isin(selected)]
        if 'locations' in event:
            selected = string_list(event['locations'], 'locations')
            destinations = n.links.loc[names, 'bus1']
            locations = destinations.map(n.buses.location)
            names = names[locations.isin(selected).to_numpy() | destinations.isin(selected).to_numpy()]
        if names.empty:
            raise ValueError(f'No {fuel} consuming Links match event {name}.')
        table = n.links.loc[names]
        if table.p_nom_extendable.any() or table.committable.any():
            raise ValueError('Freeze continuous fuel-consuming Link capacities before applying disruptions.')
        if not np.isfinite(table.p_nom).all() or table.p_nom.lt(0).any():
            raise ValueError('Fuel-use capacities must be finite and nonnegative.')
        hours = snapshots[(snapshots >= start) & (snapshots < end)]
        for generator in names:
            used = occupied.get(generator, pd.DatetimeIndex([]))
            if len(used.intersection(hours)):
                raise ValueError(f'Overlapping fuel-use disruptions for {generator}.')
            occupied[generator] = used.union(hours)
            upper = normal.loc[hours, generator]
            lower = minimum.loc[hours, generator]
            restricted = upper * (1 - reduction)
            if (not np.isfinite(upper).all() or not np.isfinite(lower).all()
                    or upper.lt(0).any() or lower.gt(restricted + 1e-12).any()):
                raise ValueError(f'Invalid availability or must-run conflict for {generator}.')
            if generator not in profile:
                profile[generator] = normal[generator]
            profile.loc[hours, generator] = restricted
            rating = float(table.at[generator, 'p_nom'])
            records.append({'event': name, 'fuel': fuel, 'link': generator,
                            'bus0': table.at[generator, 'bus0'], 'bus1': table.at[generator, 'bus1'], 'country': countries[generator],
                            'start': str(start), 'end': str(end), 'hours': len(hours),
                            'reduction': float(reduction), 'remaining_fraction': float(1-reduction),
                            'p_nom_MW': rating,
                            'normal_min_capacity_MW': float(upper.min() * rating),
                            'normal_max_capacity_MW': float(upper.max() * rating),
                            'restricted_min_capacity_MW': float(restricted.min() * rating),
                            'restricted_max_capacity_MW': float(restricted.max() * rating)})
    # No network input changes until all events pass validation.
    n.links_t.p_max_pu = profile
    n.meta = {**n.meta, META_KEY: records}


def fuel_link_disruption_report(n):
    return pd.DataFrame(n.meta.get(META_KEY, []), columns=REPORT_COLUMNS)
