import pypsa
import pandas as pd
import numpy as np
from pathlib import Path

from scripts.household_heat_constraints import META_KEY, freeze_household_heat_network
from scripts.heat_vent_capacity import prepare_dispatch_heat_vent_capacity


CAPACITIES = {
    "Generator": "p_nom", "Link": "p_nom", "StorageUnit": "p_nom",
    "Store": "e_nom", "Line": "s_nom", "Transformer": "s_nom",
}

FUEL_ACCOUNTING_END_USES = {
    "land transport oil": "land transport",
    "gas for industry": "industry",
    "coal for industry": "industry",
    "solid biomass for industry": "industry",
    "industry methanol": "industry",
    "kerosene for aviation": "aviation",
    "shipping oil": "shipping",
    "shipping methanol": "shipping",
    "agriculture machinery oil": "agriculture machinery",
}


def add_fuel_backup_links(n, options):
    """Resize original accounting links; retain the legacy API/config name.

    capacity_multiplier specifies additional capacity relative to the original,
    as it did for parallel backups: 1.0 now doubles the original link directly.
    No backup link or marginal-cost premium is added.
    """
    for name, info in n.meta.get("fuel_delivery_capacity_adjustments", {}).items():
        if name in n.links.index:
            for attr, value in info["original_capacities"].items():
                n.links.at[name, attr] = value
    old = list(n.meta.get("fuel_backup_links", {}))
    n.remove("Link", n.links.index.intersection(old))
    n.meta = {**n.meta, "fuel_backup_links": {}, "fuel_delivery_capacity_adjustments": {}}
    if not options.get("enable", False):
        return
    multiplier = float(options.get("capacity_multiplier", 1.0))
    if not np.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("Additional fuel-delivery capacity multiplier must be positive and finite.")

    carriers = options.get("carriers", list(FUEL_ACCOUNTING_END_USES))
    originals = n.links.index[n.links.carrier.isin(carriers)]
    metadata = {}
    for original in originals:
        row = n.links.loc[original]
        if not np.isfinite(row.p_nom) or row.p_nom < 0:
            raise ValueError(f"Invalid fixed fuel-delivery capacity: {original}")
        if row.p_nom == 0:
            continue
        if row.p_nom_extendable:
            raise ValueError(f"Fuel-delivery capacity must be fixed before resizing: {original}")
        attrs = [attr for attr in ("p_nom", "p_nom_opt", "p_nom_min", "p_nom_max")
                 if attr in n.links.columns]
        capacities = {attr: float(row[attr]) for attr in attrs}
        for attr, value in capacities.items():
            n.links.at[original, attr] = value * (1 + multiplier)
        metadata[original] = {
            "original_capacities": capacities,
            "capacity_multiplier": 1 + multiplier,
        }
    n.meta = {**n.meta, "fuel_delivery_capacity_adjustments": metadata}


def assert_fixed_capacities(n):
    for component, attr in CAPACITIES.items():
        table = n.components[component].static
        if table[attr + "_extendable"].any():
            raise ValueError(f"Dispatch must not contain extendable {component} assets.")


def apply_low_voltage_capacity_multipliers(n, multipliers):
    """Scale fixed distribution links by country, including reverse-direction links."""
    if not multipliers:
        return
    if not isinstance(multipliers, dict):
        raise ValueError("Low-voltage capacity multipliers must be a country-to-factor mapping.")
    if "low_voltage_capacity_adjustments" in n.meta:
        raise ValueError("Low-voltage capacity multipliers have already been applied.")
    records = []
    links = n.links.loc[n.links.carrier.eq("electricity distribution grid")]
    for country, factor in multipliers.items():
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not np.isfinite(factor) or factor <= 0:
            raise ValueError("Low-voltage capacity multipliers must be positive finite numbers.")
        selected = []
        for name, row in links.iterrows():
            endpoints = n.buses.loc[[row.bus0, row.bus1]]
            if set(endpoints.carrier) != {"AC", "low voltage"}:
                continue
            low_voltage = endpoints.loc[endpoints.carrier.eq("low voltage")].iloc[0]
            if low_voltage.get("country", "") == country:
                selected.append(name)
        if not selected:
            raise ValueError(f"No low-voltage distribution links found for country {country!r}.")
        for name in selected:
            original = float(n.links.at[name, "p_nom"])
            if n.links.at[name, "p_nom_extendable"] or not np.isfinite(original) or original < 0:
                raise ValueError(f"Freeze finite distribution capacities before scaling {name}.")
            records.append({"link": name, "country": country, "multiplier": float(factor),
                            "original_input_capacity_MW": original,
                            "input_capacity_MW": original * factor})
    # Validate every selection before changing any capacity.
    for record in records:
        n.links.loc[record["link"], ["p_nom", "p_nom_opt", "p_nom_min", "p_nom_max"]] = record["input_capacity_MW"]
    n.meta = {**n.meta, "low_voltage_capacity_adjustments": records}


def set_weather(n, weather):
    """Transfer operating inputs by asset name, keeping base dates and equipment."""
    for network in (n, weather):
        if not isinstance(network.snapshots, pd.DatetimeIndex):
            raise ValueError("Weather transfer supports single-period datetime snapshots only.")
    # Positional mapping is safe only for the same calendar hours and weights.
    calendar = lambda index: list(zip(index.month, index.day, index.hour, index.minute))
    if calendar(n.snapshots) != calendar(weather.snapshots):
        raise ValueError("Weather and base must have matching calendar hours; remove leap days.")
    if not np.allclose(
        n.snapshot_weightings.to_numpy(),
        weather.snapshot_weightings.reindex(columns=n.snapshot_weightings.columns).to_numpy(),
    ):
        raise ValueError("Weather and base snapshot weights differ.")

    attributes = {
        "Load": ["p_set"],
        "Generator": ["p_max_pu", "p_min_pu"],
        "StorageUnit": ["inflow", "p_max_pu", "p_min_pu"],
        "Link": ["p_max_pu", "p_min_pu", "efficiency"],
        "Store": ["e_max_pu", "e_min_pu"],
        "Line": ["s_max_pu"],
    }
    # Multi-output heat technologies can have temperature-dependent extra ports.
    attributes["Link"] += [
        key for key in weather.components["Link"].dynamic
        if key.startswith("efficiency") and key != "efficiency"
    ]
    for component, attrs in attributes.items():
        target = n.components[component]
        source = weather.components[component]
        for attr in attrs:
            if attr not in source.dynamic:
                continue
            names = target.static.index.intersection(
                target.dynamic[attr].columns.union(source.dynamic[attr].columns)
            )
            if component == "Load":
                names = target.static.index
            if component == "Generator":
                shedding = target.static.carrier.str.contains(
                    r"load[- ]?shedding|^load$", case=False, na=False,
                )
                names = names.difference(target.static.index[shedding])
            missing = names.difference(source.static.index)
            if len(missing):
                raise ValueError(f"Weather network lacks {component}.{attr}: {missing.tolist()}")
            if names.empty:
                continue
            dense = weather.get_switchable_as_dense(component, attr).reindex(columns=names)
            if not np.isfinite(dense.to_numpy()).all():
                raise ValueError(f"Non-finite weather input: {component}.{attr}")
            # Do not copy donor topology or capacities, including nuclear's static factor.
            target.dynamic[attr] = target.dynamic[attr].copy()
            target.dynamic[attr][names] = pd.DataFrame(
                dense.to_numpy(), index=n.snapshots, columns=names,
            )

    n.meta = {**(n.meta or {}), "disruption_weather": {
        "start": str(weather.snapshots[0]), "end": str(weather.snapshots[-1]),
        "mapping": "matching calendar hours; original base date labels retained",
    }}


def household_demands(n):
    """Demand at rewired technology buses, which have no explicit PyPSA Loads."""
    settings = n.meta.get(META_KEY)
    if not settings:
        return {}
    if settings.get("mode") != "dispatch":
        raise ValueError("Freeze household heat shares before adding shedding.")
    loads = n.get_switchable_as_dense("Load", "p_set")
    demands = {}
    for group in settings["groups"]:
        demand = loads[group["demand_loads"]].sum(axis=1) * settings["shares"][group["group"]]
        demand += loads[group.get("additional_demand_loads", [])].sum(axis=1)
        delivery = group["delivery"]
        bus = n.links.at[delivery, "bus0"]
        demands[bus] = demands.get(bus, 0) + demand
        # Delivery links are bookkeeping, not installed heat-production capacity.
        n.links.at[delivery, "p_nom"] = max(n.links.at[delivery, "p_nom"], demand.max())
    return demands

def add_load_shedding(n, elec_cost=10_000, heat_cost=10_000):
    # Includes both static and time-varying load values.
    demand = n.get_switchable_as_dense("Load", "p_set")

    # Sum all loads connected to each bus at every snapshot.
    demand_by_bus = demand.T.groupby(n.loads.bus).sum().T
    for bus, profile in household_demands(n).items():
        demand_by_bus[bus] = profile
    # Household delivery constraints already allocate demand to technology buses.
    # Shedding belongs at those buses, not a second time at the receiving heat bus.
    allocated = [g["heat_bus"] for g in n.meta.get(META_KEY, {}).get("groups", [])]
    demand_by_bus = demand_by_bus.drop(columns=allocated, errors="ignore")
    peak_demand = demand_by_bus.max().clip(lower=0)

    sectors = {
        "electricity": (["AC", "low voltage"], elec_cost),
        "heat": (
            n.buses.carrier[n.buses.carrier.str.endswith(" heat")].unique().tolist(),
            heat_cost,
        ),
    }

    # Recreate only our own generators so repeated preparation refreshes limits.
    old = n.generators.index[n.generators.carrier.isin(
        ["load-shedding-electricity", "load-shedding-heat"]
    )]
    n.remove("Generator", old)

    for sector, (bus_carriers, cost) in sectors.items():
        buses = n.buses.index[
            n.buses.carrier.isin(bus_carriers)
        ].intersection(peak_demand.index)
        buses = buses[peak_demand.reindex(buses).gt(0)]

        if buses.empty:
            continue

        carrier = f"load-shedding-{sector}"
        if carrier not in n.carriers.index:
            n.add("Carrier", carrier)

        n.add(
            "Generator",
            buses,
            suffix=f" load shedding {sector}",
            bus=buses,
            carrier=carrier,
            p_nom=peak_demand.reindex(buses).to_numpy(),
            p_nom_extendable=False,
            marginal_cost=cost,
            sign=1.0,
            p_max_pu=(demand_by_bus[buses].clip(lower=0) / peak_demand[buses]).to_numpy(),
        )


if __name__ == "__main__":
    n = pypsa.Network(snakemake.input.net)

    shares = snakemake.input.get("household_shares", [])
    if META_KEY in n.meta:
        if not shares:
            raise ValueError("This base network requires its matching household heat-share CSV.")
        freeze_household_heat_network(n, shares)
    else:
        n.optimize.fix_optimal_capacities()

    weather_file = snakemake.input.get("weather", [])
    if weather_file:
        set_weather(n, pypsa.Network(weather_file))

    apply_low_voltage_capacity_multipliers(
        n, snakemake.params.get("low_voltage_capacity_multipliers", {}),
    )

    if snakemake.params.get("rolling_horizon", False):
        from scripts.disruption_waste_hvc import prepare_waste_hvc
        prepare_waste_hvc(n, snakemake.params.rolling.get("waste_hvc", {}))

    add_fuel_backup_links(n, dict(snakemake.params.fuel_backup))
    # Refresh accounting sinks after freezing capacities and transferring weather.
    # Also repairs old references whose optimized vent size reflected baseline use.
    prepare_dispatch_heat_vent_capacity(n, snakemake.params.get("heat_vent", {}))
    add_load_shedding(n, **dict(snakemake.params.load_shedding))
    assert_fixed_capacities(n)
    n.meta = {**n.meta, "disruption_preparation": {
        "base_network": str(snakemake.input.net),
        "weather_network": str(weather_file) if weather_file else None,
        "dispatch_only": True,
    }}
    Path(snakemake.output.net).parent.mkdir(parents=True, exist_ok=True)
    n.export_to_netcdf(snakemake.output.net)
