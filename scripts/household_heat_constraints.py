# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
# SPDX-License-Identifier: MIT
"""Optimise household heating shares with technology-specific water tanks.

Overnight, single-period networks only. All household groups may invest in thermal
storage supporting the model's combined space-heat and hot-water demand. Its
capacity is optimised and can be zero; no minimum tank ownership/size is imposed.
Unmerged services and all district heating are untouched. For merged rural and
urban decentral buses, shares apply only to preserved residential demand; ALL
remaining demand (services/agriculture) is assigned to the gas group. This is an
explicit modelling assumption. Gas boilers retain their upstream extension flags.
Shares describe useful-heat demand, not household counts.

"""

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr


META_KEY = "household_heat_allocation"
SHARE_VARIABLE = "Household-heat-share"
HEAT_CARRIERS = (
    "residential rural heat",
    "residential urban decentral heat",
    "rural heat",
    "urban decentral heat",
)
TECHNOLOGIES = (
    "air heat pump", "ground heat pump", "gas boiler", "oil boiler",
    "biomass boiler", "resistive heater",
)


def _demand(n, bus, snapshots=None, loads=None):
    snapshots = n.snapshots if snapshots is None else snapshots
    loads = n.loads.index[n.loads.bus.eq(bus)] if loads is None else pd.Index(loads)
    if loads.empty:
        raise ValueError(f"No heat load at {bus}.")
    if not n.loads.loc[loads, "sign"].eq(-1).all():
        raise ValueError(f"Nonstandard load signs at {bus} are unsupported.")
    return n.get_switchable_as_dense("Load", "p_set", snapshots)[loads].sum(axis=1)


def _clone(n, component, source, target, changes):
    # Preserve input attributes and input time series; discard solved outputs.
    c = n.components[component]
    attrs = c.defaults
    inputs = attrs.index[attrs.status.str.startswith("Input")]
    values = c.static.loc[source, c.static.columns.intersection(inputs)].to_dict()
    for attr, frame in c.dynamic.items():
        if attr in inputs and source in frame.columns:
            values[attr] = frame[source].copy()
    values.update(changes)
    if component == "Link" and "reversed" in c.static.columns:
        # PyPSA-Eur adds this custom field outside PyPSA's input schema.
        # Omitting it makes new rows NaN and breaks Boolean Link selections.
        source_reversed = c.static.at[source, "reversed"]
        values["reversed"] = bool(source_reversed) if pd.notna(source_reversed) else False
    n.add(component, target, **values)


def _existing_heat_allocation(n, heaters):
    """Translate installed equipment into fixed useful-heat demand shares."""
    thermal = {}
    for tech, name, port in heaters:
        row = n.links.loc[name]
        installed = float(row.p_nom_min if row.p_nom_extendable else row.p_nom)
        if not np.isfinite(installed) or installed < 0:
            raise ValueError(f"Invalid existing heater capacity for {name}.")
        if port == "bus1":
            if name in n.links_t.efficiency.columns:
                raise ValueError(f"{name}: capacity-share conversion requires constant boiler efficiency.")
            efficiency = float(row.efficiency)
            if not np.isfinite(efficiency) or efficiency <= 0:
                raise ValueError(f"Invalid heater efficiency for {name}.")
            installed *= efficiency
        # Heat-pump bus0 nominal rating is already thermal, not electrical.
        thermal[tech] = installed
    total = sum(thermal.values())
    fixed = {tech: value / total for tech, value in thermal.items()} if total > 0 else {}
    return thermal, fixed


def prepare_household_heat_network(n, config):
    """Rewire heaters and clone per-technology tanks before model creation."""
    opts = config.get("sector", {}).get(META_KEY, {})
    if not opts.get("enable", False):
        return
    if META_KEY in n.meta:
        raise ValueError("Household topology already exists; do not prepare it twice.")
    if isinstance(n.snapshots, pd.MultiIndex):
        raise ValueError("Only single-period overnight networks are supported.")
    if config.get("solving", {}).get("options", {}).get("rolling_horizon", False):
        raise ValueError("Household allocation currently requires a full-horizon solve.")
    if config.get("sector", {}).get("residential_heat", {}).get("dsm", {}).get("enable", False):
        raise ValueError("Disable residential building DSM for this formulation.")

    obsolete = {"storage_hours", "tank_power_per_peak"}.intersection(opts)
    if obsolete:
        raise ValueError(f"Remove obsolete prescribed tank-sizing settings: {sorted(obsolete)}.")
    default_buses = n.buses.index[n.buses.carrier.isin(HEAT_CARRIERS)]
    buses = list(opts.get("heat_buses", default_buses))
    if not buses or len(set(buses)) != len(buses):
        raise ValueError("Select unique residential or merged rural/decentral heat buses.")

    plans = []
    for bus in buses:
        if bus not in n.buses.index or n.buses.at[bus, "carrier"] not in HEAT_CARRIERS:
            raise ValueError(f"{bus}: expected a residential or merged rural/decentral heat bus.")
        all_loads = n.loads.index[n.loads.bus.eq(bus)]
        merged = n.buses.at[bus, "carrier"] in ("rural heat", "urban decentral heat")
        if merged:
            residential_loads = all_loads[all_loads.str.endswith(
                (" residential rural heat", " residential urban decentral heat")
            )]
            if residential_loads.empty:
                raise ValueError(f"{bus}: missing preserved residential demand. Rebuild with "
                                 "household_heat_allocation.enable=true before clustering.")
        else:
            residential_loads = all_loads
        other_loads = all_loads.difference(residential_loads)
        demand = _demand(n, bus)
        if not np.isfinite(demand).all() or (demand < 0).any():
            raise ValueError(f"Invalid heat demand at {bus}.")
        peak = float(demand.max())
        if peak == 0:
            continue
        prefix = bus.removesuffix(" heat")
        tank = prefix + " water tanks"
        charger, discharger = tank + " charger", tank + " discharger"
        if tank not in n.stores.index or tank not in n.buses.index:
            raise ValueError(f"{bus}: enable sector.tes to create water-tank templates.")
        if charger not in n.links.index or discharger not in n.links.index:
            raise ValueError(f"{bus}: missing tank charger/discharger.")
        ratio = float(n.links.loc[charger].get("energy to power ratio", np.nan))
        if not np.isfinite(ratio) or ratio <= 0:
            raise ValueError(f"{bus}: missing/invalid cost-data tank energy-to-power ratio; "
                             "populate the tank template ratio from technology costs.")
        for component, name, capacity in (
            ("Store", tank, "e_nom"), ("Link", charger, "p_nom"),
            ("Link", discharger, "p_nom"),
        ):
            row = n.components[component].static.loc[name]
            if row[capacity] > 0 or row[capacity + "_min"] > 0:
                raise ValueError(f"{name}: existing tank capacities require an explicit allocation.")
        if n.links.at[charger, "bus0"] != bus or n.links.at[charger, "bus1"] != tank:
            raise ValueError(f"Unexpected charger topology at {bus}.")
        if n.links.at[discharger, "bus0"] != tank or n.links.at[discharger, "bus1"] != bus:
            raise ValueError(f"Unexpected discharger topology at {bus}.")
        if discharger in n.links_t.efficiency.columns:
            raise ValueError("Time-varying tank discharger efficiency is unsupported.")
        eta = float(n.links.at[discharger, "efficiency"])
        if not 0 < eta <= 1:
            raise ValueError(f"Invalid tank discharger efficiency at {bus}.")

        heaters = []
        for tech in TECHNOLOGIES:
            name = prefix + " " + tech
            if name not in n.links.index:
                continue
            output_port = "bus0" if tech.endswith("heat pump") else "bus1"
            if n.links.at[name, output_port] != bus:
                raise ValueError(f"Unexpected heat-output port for {name}.")
            heaters.append((tech, name, output_port))
        if not heaters:
            raise ValueError(f"No supported primary heating Links at {bus}.")
        if len(other_loads) and "gas boiler" not in [tech for tech, _, _ in heaters]:
            raise ValueError(f"{bus}: a gas boiler is required for services/agricultural demand.")
        existing_thermal, fixed_shares = _existing_heat_allocation(n, heaters)

        connected = n.links.index[
            n.links.filter(regex=r"^bus\d+$").eq(bus).any(axis=1)
            | n.links.filter(regex=r"^bus\d+$").eq(tank).any(axis=1)
        ]
        allowed = {charger, discharger, *(name for _, name, _ in heaters)}
        unexpected = connected.difference(list(allowed))
        if len(unexpected):
            raise ValueError(f"Unallocated Links at {bus}: {unexpected.tolist()}.")
        auxiliary = n.generators.index[n.generators.bus.eq(bus)]
        removable = n.generators.loc[auxiliary].apply(
            lambda row: ("solar thermal" in row.carrier or "heat vent" in row.carrier)
            and row.p_nom == 0 and row.p_nom_min == 0, axis=1,
        )
        if len(auxiliary) and (not opts.get("remove_unbuilt_auxiliaries", False) or not removable.all()):
            raise ValueError(
                f"Unallocated Generators at {bus}: {auxiliary.tolist()}. Disable them "
                "or explicitly set remove_unbuilt_auxiliaries=true for unbuilt solar/vents."
            )
        if n.stores.bus.eq(bus).any() or n.storage_units.bus.isin([bus, tank]).any():
            raise ValueError(f"Unallocated storage/building DSM at {bus}.")
        if n.loads.bus.eq(tank).any() or n.generators.bus.eq(tank).any():
            raise ValueError(f"Unexpected equipment on tank bus {tank}.")
        if list(n.stores.index[n.stores.bus.eq(tank)]) != [tank]:
            raise ValueError(f"Unexpected Stores at {tank}.")
        plans.append((bus, peak, tank, charger, discharger, eta, ratio, heaters, auxiliary,
                      existing_thermal, fixed_shares, residential_loads, other_loads, merged))

    if not plans:
        raise ValueError("No positive household heat demand selected.")
    groups = []
    for (bus, peak, tank, charger, discharger, eta, ratio, heaters, auxiliary,
         existing_thermal, fixed_shares, residential_loads, other_loads, merged) in plans:
        for tech, heater, port in heaters:
            group = bus + " | " + tech
            group_bus = group + " household heat"
            group_tank = group + " water tanks"
            delivery = group + " demand delivery"
            n.add("Bus", group_bus, carrier=n.buses.at[bus, "carrier"],
                  location=n.buses.at[bus, "location"], unit="MWh_th")
            _clone(n, "Bus", tank, group_tank, {})
            _clone(n, "Store", tank, group_tank, {
                "bus": group_tank, "e_cyclic": True, "e_initial": 0,
                "e_nom": 0, "e_nom_min": 0,
                "e_nom_extendable": True,
            })
            _clone(n, "Link", charger, group_tank + " charger", {
                "bus0": group_bus, "bus1": group_tank,
                "p_nom": 0, "p_nom_min": 0,
                "p_nom_extendable": True, "energy to power ratio": ratio,
            })
            _clone(n, "Link", discharger, group_tank + " discharger", {
                "bus0": group_tank, "bus1": group_bus,
                "p_nom": 0, "p_nom_min": 0,
                "p_nom_extendable": True,
            })
            n.links.at[heater, port] = group_bus
            # This bookkeeping Link delivers assigned demand to the original bus.
            # Its flow equals share * demand; household bus balance supplies the
            # heater + discharge - charge equality without changing PyPSA loads.
            n.add("Link", delivery, bus0=group_bus, bus1=bus, efficiency=1,
                  carrier="household heat delivery", p_nom=peak, p_nom_extendable=False,
                  **({"reversed": False} if "reversed" in n.links.columns else {}))
            groups.append({"group": group, "heat_bus": bus, "technology": tech,
                           "heater": heater, "delivery": delivery, "tank": group_tank,
                           "peak": peak, "tank_energy_to_power_ratio": ratio,
                           "merged": merged, "demand_loads": residential_loads.tolist(),
                           "additional_demand_loads": other_loads.tolist() if tech == "gas boiler" else [],
                           "existing_thermal_capacity_MW": existing_thermal[tech],
                           "fixed_share": fixed_shares.get(tech),
                           "share_source": "existing capacity" if fixed_shares else "optimised",
                           "discharger_efficiency": eta})
        n.remove("Generator", auxiliary)
        n.remove("Link", [charger, discharger])
        n.remove("Store", tank)
        n.remove("Bus", tank)
    if "household heat delivery" not in n.carriers.index:
        n.add("Carrier", "household heat delivery")
    n.meta = {**n.meta, META_KEY: {"groups": groups, "mode": "investment"}}


def _select(variable, label):
    """Select and drop the component coordinate (PyPSA 0.x and 1.x)."""
    dims = [dim for dim in variable.dims if dim != "snapshot"]
    if len(dims) != 1:
        raise ValueError(f"Unexpected variable dimensions: {variable.dims}.")
    return variable.sel({dims[0]: label}, drop=True)


def _capacity(n, component, name, attr):
    row = n.components[component].static.loc[name]
    if row[attr + "_extendable"]:
        return _select(n.model[f"{component}-{attr}"], name)
    return float(row[attr])


def add_household_heat_constraints(n, snapshots):
    """Add linear allocation and tank-sizing constraints AFTER create_model()."""
    settings = n.meta.get(META_KEY)
    if settings is None:
        return
    if isinstance(n.snapshots, pd.MultiIndex):
        raise ValueError("Multi-period allocation is unsupported.")
    groups = settings["groups"]
    index = pd.Index([g["group"] for g in groups], name="household_group")
    dispatch_mode = settings["mode"] == "dispatch"
    if dispatch_mode:
        fixed = settings.get("shares", {})
        if set(fixed) != set(index) or not np.isfinite(list(fixed.values())).all():
            raise ValueError("Dispatch requires saved shares for every household group.")
        lower = upper = xr.DataArray([fixed[key] for key in index], coords=[index])
    else:
        lower = xr.DataArray([g.get("fixed_share") if g.get("fixed_share") is not None else 0
                              for g in groups], coords=[index])
        upper = xr.DataArray([g.get("fixed_share") if g.get("fixed_share") is not None else 1
                              for g in groups], coords=[index])
    shares = n.model.add_variables(lower=lower, upper=upper, coords=[index], name=SHARE_VARIABLE)
    snapshot_index = pd.Index(snapshots, name="snapshot")
    for i, bus in enumerate(dict.fromkeys(g["heat_bus"] for g in groups)):
        keys = [g["group"] for g in groups if g["heat_bus"] == bus]
        n.model.add_constraints(shares.sel(household_group=keys).sum() == 1,
                                name=f"Household-share-sum-{i}")
    for i, g in enumerate(groups):
        share = shares.sel(household_group=g["group"], drop=True)
        demand = xr.DataArray(_demand(n, g["heat_bus"], snapshots,
                                     loads=g.get("demand_loads")).values, coords=[snapshot_index])
        additional = 0
        if g.get("additional_demand_loads"):
            additional = xr.DataArray(_demand(n, g["heat_bus"], snapshots,
                                              loads=g["additional_demand_loads"]).values,
                                      coords=[snapshot_index])
        flow = _select(n.model["Link-p"], g["delivery"]).sel(snapshot=snapshots)
        n.model.add_constraints(flow == share * demand + additional,
                                name=f"Household-demand-{i}")
        if dispatch_mode:
            # Capacities are already fixed; do not re-optimise tanks in contingencies.
            continue
        e_nom = _capacity(n, "Store", g["tank"], "e_nom")
        c_nom = _capacity(n, "Link", g["tank"] + " charger", "p_nom")
        r_nom = _capacity(n, "Link", g["tank"] + " discharger", "p_nom")
        n.model.add_constraints(e_nom == c_nom * g["tank_energy_to_power_ratio"],
                                name=f"Household-tank-energy-to-power-{i}")
        n.model.add_constraints(c_nom == r_nom * g["discharger_efficiency"],
                                name=f"Household-tank-charger-ratio-{i}")


def normalise_household_share_table(table, tolerance=1e-4):
    """Correct only solver-scale share round-off; preserve raw results for audit.

    Barrier solutions can satisfy the full model's scaled tolerances while
    shares sum to 1 +/- 1e-5. Exact fixed shares must sum to one in dispatch.
    Larger inconsistencies remain errors rather than being silently rescaled.
    """
    table = table.copy()
    shares = pd.to_numeric(table["share"], errors="raise")
    if not np.isfinite(shares).all() or (shares < -tolerance).any() or (shares > 1 + tolerance).any():
        raise ValueError("Share values are outside the permitted solver-round-off tolerance.")
    totals = shares.groupby(table["heat_bus"]).transform("sum")
    if not np.allclose(totals, 1, rtol=0, atol=tolerance):
        raise ValueError("Share values must sum to one at each heat bus within solver tolerance.")
    if "raw_share" not in table.columns:
        table["raw_share"] = shares
    shares = shares.clip(0, 1)
    table["share"] = shares / shares.groupby(table["heat_bus"]).transform("sum")
    return table


def save_household_heat_shares(n, csv_path):
    """Write solved household shares and equipment identifiers to a separate CSV."""
    if META_KEY not in n.meta:
        return None
    solved = n.model[SHARE_VARIABLE].solution.to_series()
    if not np.isfinite(solved).all():
        raise ValueError("Cannot save shares without a finite successful solution.")
    result = pd.DataFrame(n.meta[META_KEY]["groups"])
    result["share"] = result.group.map(solved)
    result = normalise_household_share_table(result)
    result["tank_capacity_MWh"] = result.tank.map(n.stores.e_nom_opt)
    result["heater_p_nom_opt"] = result.heater.map(n.links.p_nom_opt)
    result["charger_input_capacity_MW"] = (result.tank + " charger").map(n.links.p_nom_opt)
    result["discharger_heat_capacity_MW"] = (
        (result.tank + " discharger").map(n.links.p_nom_opt) * result.discharger_efficiency
    )
    result.to_csv(Path(csv_path), index=False)
    return result


def freeze_household_heat_network(n, shares_csv):
    """Fix base capacities and read household shares from the matching CSV."""
    if META_KEY not in n.meta:
        raise ValueError("Load the base network containing household group equipment first.")
    table = pd.read_csv(Path(shares_csv))
    required = {"group", "share", "heat_bus", "technology", "heater", "delivery", "tank"}
    if not required.issubset(table.columns):
        raise ValueError(f"Share CSV is missing columns: {sorted(required - set(table.columns))}.")
    if table.group.isna().any() or table.group.duplicated().any():
        raise ValueError("Share CSV must contain exactly one row per household group.")
    expected = pd.DataFrame(n.meta[META_KEY]["groups"]).set_index("group")
    table = table.set_index("group")
    if set(table.index) != set(expected.index):
        raise ValueError("Share CSV household groups do not match the base network.")
    table = table.reindex(expected.index)
    for column in required - {"group", "share"}:
        if not table[column].eq(expected[column]).all():
            raise ValueError(f"Share CSV {column} identifiers do not match the base network.")
    shares = normalise_household_share_table(table).share
    for component, attr in (
        ("Generator", "p_nom"), ("Link", "p_nom"), ("Store", "e_nom"),
        ("StorageUnit", "p_nom"), ("Line", "s_nom"), ("Transformer", "s_nom"),
    ):
        table = n.components[component].static
        extendable = table[attr + "_extendable"]
        solved = table.loc[extendable, attr + "_opt"]
        if not np.isfinite(solved).all():
            raise ValueError(f"Missing solved {component} capacities.")
        table.loc[extendable, attr] = solved
        table.loc[extendable, attr + "_extendable"] = False
    # Runtime inputs only: no second network is needed to retrieve shares.
    n.meta = {**n.meta, META_KEY: {**n.meta[META_KEY], "mode": "dispatch",
                                "shares": shares.to_dict()}}


def household_heat_constraints(n, snapshots, snakemake):
    """Optional custom_extra_functionality entry point; topology must exist first."""
    if META_KEY not in n.meta:
        raise ValueError("Call prepare_household_heat_network BEFORE creating the model.")
    add_household_heat_constraints(n, snapshots)
