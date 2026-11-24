# SPDX-License-Identifier: MIT
"""Dated primary-fuel supply deratings; never derate consuming/transport Links."""
from collections.abc import Mapping
from numbers import Real

import numpy as np
import pandas as pd


META_KEY = 'fuel_generator_disruptions'
REPORT_COLUMNS = ['event', 'fuel', 'generator', 'bus', 'country', 'start', 'end',
                  'hours', 'reduction', 'remaining_fraction', 'p_nom_MW',
                  'normal_min_capacity_MW', 'normal_max_capacity_MW',
                  'restricted_min_capacity_MW', 'restricted_max_capacity_MW']


def supply_generators(n, fuel):
    """Select by BOTH Generator carrier and input bus carrier, excluding shedding."""
    bus_carrier = n.generators.bus.map(n.buses.carrier)
    if fuel == 'gas':
        mask = n.generators.carrier.eq('gas') & bus_carrier.eq('gas')
    elif fuel == 'oil':
        mask = (n.generators.carrier.isin(['oil primary', 'oil'])
                & bus_carrier.isin(['oil primary', 'oil']))
    else:
        raise ValueError("Fuel supply disruptions support only 'gas' or 'oil'.")
    return n.generators.index[mask]


def string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f'{label} must be a list of nonempty strings.')
    if len(value) != len(set(value)):
        raise ValueError(f'{label} contains duplicate names.')
    return value


def generator_country(n, names):
    buses = n.buses.loc[n.generators.loc[names, 'bus']]
    # Regional supply buses can have only a location, rather than country metadata.
    country = buses.get('country', pd.Series('', index=buses.index)).fillna('')
    location = buses.get('location', pd.Series('', index=buses.index)).fillna('')
    fallback = location.where(location.ne(''), buses.index.to_series()).str[:2]
    return pd.Series(country.where(country.ne(''), fallback).to_numpy(), index=names)


def apply_fuel_generator_disruptions(n, events):
    """Install [start,end) availability reductions once, after weather transfer.

    Events may select all supply, exact supply Generator names, and/or countries.
    Country selection on a shared EU oil bus is intentionally rejected: that bus
    has no national inlet attribution. Overlapping events on one Generator are
    rejected so reductions cannot accidentally multiply each other.
    """
    if not isinstance(events, list):
        raise ValueError('fuel_generator_disruptions must be a list.')
    if not events:
        return
    downstream = {row['fuel'] for row in n.meta.get('fuel_link_disruptions', [])}
    requested = {event.get('fuel') for event in events if isinstance(event, Mapping)}
    if downstream.intersection(requested):
        raise ValueError('Cannot combine upstream supply and consuming-Link reductions for the same fuel.')
    if n.meta.get(META_KEY):
        raise ValueError('Fuel supply disruptions already applied; start from a fresh prepared network.')
    snapshots = n.snapshots
    if (not isinstance(snapshots, pd.DatetimeIndex) or snapshots.empty
            or snapshots.tz is not None or not snapshots.is_unique
            or not snapshots.is_monotonic_increasing):
        raise ValueError('Fuel disruptions require unique increasing timezone-naive datetime snapshots.')
    if len(snapshots) > 1 and not ((snapshots[1:] - snapshots[:-1]) == pd.Timedelta(hours=1)).all():
        raise ValueError('Fuel disruptions require consecutive hourly snapshots.')
    end_of_network = snapshots[-1] + pd.Timedelta(hours=1)
    normal = n.get_switchable_as_dense('Generator', 'p_max_pu')
    minimum = n.get_switchable_as_dense('Generator', 'p_min_pu')
    profile = n.generators_t.p_max_pu.reindex(snapshots).copy()
    occupied, records, event_names = {}, [], set()
    for i, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise ValueError('Each fuel disruption must be a mapping.')
        unknown = set(event) - {'name', 'fuel', 'reduction', 'start', 'end', 'generators', 'countries'}
        if unknown:
            raise ValueError(f'Unknown fuel disruption settings: {sorted(unknown)}')
        fuel = event.get('fuel')
        names = supply_generators(n, fuel)
        name = event.get('name', f'{fuel}_supply_{i}')
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
        if 'generators' in event:
            explicit = pd.Index(string_list(event['generators'], 'generators'))
            invalid = explicit.difference(names)
            if len(invalid):
                raise ValueError(f'Selected names are not {fuel} supply Generators: {invalid.tolist()}')
            names = explicit
        countries = generator_country(n, names)
        if 'countries' in event:
            selected = string_list(event['countries'], 'countries')
            if fuel == 'oil' and countries.eq('EU').any():
                raise ValueError('Shared EU oil supply cannot be restricted by individual country; omit countries.')
            names = names[countries.isin(selected)]
        if names.empty:
            raise ValueError(f'No {fuel} supply Generators match event {name}.')
        table = n.generators.loc[names]
        if table.p_nom_extendable.any() or table.committable.any():
            raise ValueError('Freeze continuous fuel supply Generator capacities before applying disruptions.')
        if not np.isfinite(table.p_nom).all() or table.p_nom.lt(0).any():
            raise ValueError('Fuel supply capacities must be finite and nonnegative.')
        if not table.sign.eq(1).all():
            raise ValueError('Fuel supply Generators must have sign=1.')
        hours = snapshots[(snapshots >= start) & (snapshots < end)]
        for generator in names:
            used = occupied.get(generator, pd.DatetimeIndex([]))
            if len(used.intersection(hours)):
                raise ValueError(f'Overlapping supply disruptions for {generator}.')
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
            records.append({'event': name, 'fuel': fuel, 'generator': generator,
                            'bus': table.at[generator, 'bus'], 'country': countries[generator],
                            'start': str(start), 'end': str(end), 'hours': len(hours),
                            'reduction': float(reduction), 'remaining_fraction': float(1-reduction),
                            'p_nom_MW': rating,
                            'normal_min_capacity_MW': float(upper.min() * rating),
                            'normal_max_capacity_MW': float(upper.max() * rating),
                            'restricted_min_capacity_MW': float(restricted.min() * rating),
                            'restricted_max_capacity_MW': float(restricted.max() * rating)})
    # No network input changes until all events pass validation.
    n.generators_t.p_max_pu = profile
    n.meta = {**n.meta, META_KEY: records}


def fuel_generator_disruption_report(n):
    return pd.DataFrame(n.meta.get(META_KEY, []), columns=REPORT_COLUMNS)
