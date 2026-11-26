# SPDX-License-Identifier: MIT
"""Apply scheduled interconnector outages after weather/capacity preparation."""
from collections.abc import Mapping

import networkx as nx
import numpy as np
import pandas as pd


def bridge_line_names(n):
    """Only AC bridges can be blocked without changing cycle-based KVL."""
    graph = nx.MultiGraph()
    for component in ("Line", "Transformer"):
        for name, row in n.components[component].static.iterrows():
            graph.add_edge(row.bus0, row.bus1, key=(component, name))
    bridges = {frozenset(edge) for edge in nx.bridges(graph)}
    return {
        name for name, row in n.lines.iterrows()
        if frozenset((row.bus0, row.bus1)) in bridges
    }


def selected_assets(n, event, component, key):
    values = event.get(key, [])
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError(f"{key} must be a list of exact string asset names.")
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate asset names in {key}.")
    names = pd.Index(values)
    table = n.components[component].static
    missing = names.difference(table.index)
    if len(missing):
        raise ValueError(f"Unknown {component} names: {missing.tolist()}")
    if names.empty:
        return names
    selected = table.loc[names]
    for port in ("bus0", "bus1"):
        if not selected[port].map(n.buses.carrier).eq("AC").all():
            raise ValueError(f"Selected {component} assets must connect AC buses.")
    capacity = "s_nom" if component == "Line" else "p_nom"
    if selected[capacity + "_extendable"].any():
        raise ValueError("Freeze capacities before applying interconnector outages.")
    if not np.isfinite(selected[capacity]).all() or selected[capacity].lt(0).any():
        raise ValueError(f"Selected {component} capacities must be finite and nonnegative.")
    if component == "Link":
        if not selected.carrier.eq("DC").all():
            raise ValueError("Selected interconnector Links must have carrier 'DC'.")
        if selected.committable.any():
            raise ValueError("Committable Link outages require commitment handling.")
        extra_ports = [c for c in selected if c.startswith("bus") and c not in ("bus0", "bus1")]
        if extra_ports and selected[extra_ports].fillna("").ne("").any().any():
            raise ValueError("Interconnector outages support two-port Links only.")
    return names


def apply_interconnector_outages(n, events):
    """Block transfers in [start, end), preserving capacities and other hours.

    Dates use the prepared network's calendar, also when donor weather is used.
    The complete schedule is installed once; rolling windows select its hours.
    All input validation finishes before network inputs are changed.
    """
    if not isinstance(events, list):
        raise ValueError("interconnector_outages must be a list.")
    if not events:
        return
    snapshots = n.snapshots
    if not isinstance(snapshots, pd.DatetimeIndex) or snapshots.empty:
        raise ValueError("Interconnector outages require datetime snapshots.")
    if snapshots.tz is not None:
        raise ValueError("Use timezone-naive network-calendar timestamps.")
    if not snapshots.is_unique or not snapshots.is_monotonic_increasing:
        raise ValueError("Outage snapshots must be unique and increasing.")
    if len(snapshots) > 1 and not ((snapshots[1:] - snapshots[:-1]) == pd.Timedelta(hours=1)).all():
        raise ValueError("Interconnector outages currently require consecutive hourly snapshots.")
    network_end = snapshots[-1] + pd.Timedelta(hours=1)
    plans = []
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("Each outage must be a mapping.")
        unknown = set(event) - {"name", "start", "end", "lines", "links"}
        if unknown:
            raise ValueError(f"Unknown outage settings: {sorted(unknown)}")
        if any(event.get(key) is None for key in ("start", "end")):
            raise ValueError("Each outage requires explicit start and end dates.")
        start, end = pd.Timestamp(event["start"]), pd.Timestamp(event["end"])
        if pd.isna(start) or pd.isna(end):
            raise ValueError("Outage dates must be valid timestamps.")
        if start.tzinfo is not None or end.tzinfo is not None:
            raise ValueError("Outage dates must be timezone-naive.")
        if not start < end:
            raise ValueError("Outage start must precede end.")
        if start not in snapshots or (end not in snapshots and end != network_end):
            raise ValueError("Outage bounds must align with the network's hourly calendar.")
        assets = {
            component: selected_assets(n, event, component, key)
            for component, key in (("Line", "lines"), ("Link", "links"))
        }
        if all(names.empty for names in assets.values()):
            raise ValueError("Each outage must select at least one Line or Link.")
        if not assets["Line"].empty:
            unsupported = set(assets["Line"]) - bridge_line_names(n)
            if unsupported:
                raise ValueError(
                    f"Meshed AC Line outages require topology/KVL handling: {sorted(unsupported)}"
                )
        hours = snapshots[(snapshots >= start) & (snapshots < end)]
        plans.append((event, start, end, hours, assets))

    # Stage profiles so even a non-finite input cannot leave a partial change.
    profiles, records = {}, []
    for event, start, end, hours, assets in plans:
        for component, names in assets.items():
            if names.empty:
                continue
            attrs = ("s_max_pu",) if component == "Line" else ("p_min_pu", "p_max_pu")
            for attr in attrs:
                key = component, attr
                if key not in profiles:
                    profiles[key] = n.components[component].dynamic[attr].reindex(snapshots).copy()
                frame = profiles[key]
                missing = names.difference(frame.columns)
                if len(missing):
                    frame[missing] = n.get_switchable_as_dense(component, attr).loc[snapshots, missing]
                if not np.isfinite(frame.loc[:, names].to_numpy()).all():
                    raise ValueError(f"Non-finite {component}.{attr} outage input.")
                frame.loc[hours, names] = 0.0
        records.append({
            "name": str(event.get("name", "interconnector_outage")),
            "start": str(start), "end": str(end), "hours": len(hours),
            "lines": assets["Line"].tolist(), "links": assets["Link"].tolist(),
        })
    for (component, attr), frame in profiles.items():
        n.components[component].dynamic[attr] = frame
    n.meta = {**(n.meta or {}), "interconnector_outages": records}
