"""Persist directional AC exchange limits independently of physical line losses."""
import numpy as np

META_KEY = "directional_ac_limits"


def prepare_directional_ac_limits(n, rules):
    """Resolve bus pairs after clustering; preserve all original line parameters."""
    if not isinstance(rules, list):
        raise ValueError("sector.directional_ac_limits must be a list.")
    records, labels, claimed = [], set(), set()
    allowed = {"name", "from_bus", "to_bus", "forward_mw", "reverse_mw"}
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != allowed:
            raise ValueError(f"Directional AC rules require exactly {sorted(allowed)}.")
        label = rule["name"]
        if not isinstance(label, str) or not label or label in labels:
            raise ValueError("Directional AC limit names must be nonempty and unique.")
        origin, destination = rule["from_bus"], rule["to_bus"]
        if origin == destination or origin not in n.buses.index or destination not in n.buses.index:
            raise ValueError(f"Unknown or identical directional AC endpoints: {label}.")
        if not n.buses.loc[[origin, destination], "carrier"].eq("AC").all():
            raise ValueError(f"Directional AC endpoints must be AC buses: {label}.")
        limits = [rule["forward_mw"], rule["reverse_mw"]]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in limits):
            raise ValueError(f"Directional AC limits must be finite nonnegative MW: {label}.")
        if not np.isfinite(limits).all() or min(limits) < 0:
            raise ValueError(f"Directional AC limits must be finite nonnegative MW: {label}.")
        forward = n.lines.bus0.eq(origin) & n.lines.bus1.eq(destination)
        reverse = n.lines.bus1.eq(origin) & n.lines.bus0.eq(destination)
        lines = n.lines.index[forward | reverse]
        if len(lines) != 1:
            raise ValueError(f"Directional AC rule {label} must match exactly one aggregated Line; got {len(lines)}.")
        line = lines[0]
        if line in claimed:
            raise ValueError(f"Duplicate directional AC selection: {line}.")
        labels.add(label)
        claimed.add(line)
        records.append({**rule, "line": str(line), "orientation": 1 if forward.loc[line] else -1})
    n.meta = {**(n.meta or {}), META_KEY: {
        "basis": "exchange_flow", "rules": [dict(rule) for rule in rules], "limits": records,
    }}


def add_directional_ac_limit_constraints(n, snapshots):
    """Bound signed exchange (Line-s); native loss balances remain untouched."""
    metadata = (n.meta or {}).get(META_KEY, {})
    configured = getattr(n, "config", {}).get("sector", {}).get("directional_ac_limits", [])
    if configured and metadata.get("rules") != configured:
        raise ValueError("Directional AC settings differ from prepared metadata; rebuild the matching base network.")
    for number, record in enumerate(metadata.get("limits", [])):
        line = record["line"]
        if line not in n.lines.index:
            raise ValueError(f"Directional AC Line missing from network: {line}.")
        row = n.lines.loc[line]
        endpoints = (record["from_bus"], record["to_bus"])
        expected = endpoints if record["orientation"] == 1 else endpoints[::-1]
        if (row.bus0, row.bus1) != expected:
            raise ValueError(f"Directional AC Line orientation changed: {line}.")
        flow = n.model["Line-s"].sel(name=line, snapshot=snapshots) * record["orientation"]
        n.model.add_constraints(flow <= record["forward_mw"], name=f"Directional-AC-{number}-forward")
        n.model.add_constraints(flow >= -record["reverse_mw"], name=f"Directional-AC-{number}-reverse")
