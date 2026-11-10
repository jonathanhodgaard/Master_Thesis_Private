import pypsa
import pandas as pd
import numpy as np
from pathlib import Path

from scripts.household_heat_constraints import META_KEY, freeze_household_heat_network, _clone


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
    """Parallel fuel-delivery accounting links; upstream fuel is still required."""
    old = list(n.meta.get("fuel_backup_links", {}))
    n.remove("Link", n.links.index.intersection(old))
    n.meta = {**n.meta, "fuel_backup_links": {}}
    if not options.get("enable", False):
        return
    multiplier = float(options.get("capacity_multiplier", 1.0))
    premium = float(options.get("marginal_cost_premium", 0.1))
    if not np.isfinite([multiplier, premium]).all() or multiplier <= 0 or premium <= 0:
        raise ValueError("Fuel backup multiplier and marginal cost premium must be positive and finite.")

    carriers = options.get("carriers", list(FUEL_ACCOUNTING_END_USES))
    originals = n.links.index[n.links.carrier.isin(carriers)]
    costs = n.get_switchable_as_dense("Link", "marginal_cost")
    metadata = {}
    for original in originals:
        row = n.links.loc[original]
        if not np.isfinite(row.p_nom) or row.p_nom < 0:
            raise ValueError(f"Invalid fixed fuel-delivery capacity: {original}")
        if row.p_nom == 0:
            continue
        name = original + " fuel backup"
        if name in n.links.index:
            raise ValueError(f"Fuel backup name already exists: {name}")
        carrier = "fuel backup " + row.carrier
        if carrier not in n.carriers.index:
            n.add("Carrier", carrier)
        _clone(n, "Link", original, name, {
            "carrier": carrier,
            "p_nom": row.p_nom * multiplier,
            "p_nom_extendable": False,
            "p_nom_min": 0,
            "p_nom_max": row.p_nom * multiplier,
            "capital_cost": 0,
            "marginal_cost": costs[original] + premium,
        })
        destination = n.buses.loc[row.bus1]
        country = destination.get("country", "")
        if not country:
            location = str(destination.get("location", ""))
            country = location[:2] if location[:2].isalpha() else "EU"
        metadata[name] = {
            "original_link": original,
            "fuel": str(n.buses.at[row.bus0, "carrier"]),
            "end_use": FUEL_ACCOUNTING_END_USES.get(row.carrier, row.carrier),
            "country": country,
        }
    n.meta = {**n.meta, "fuel_backup_links": metadata}


def assert_fixed_capacities(n):
    for component, attr in CAPACITIES.items():
        table = n.components[component].static
        if table[attr + "_extendable"].any():
            raise ValueError(f"Dispatch must not contain extendable {component} assets.")


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

# List of carriers that are seasonal storage types
seasonal_storage_types = [
    "hydro",                     # Reservoir hydro
    "urban central water pits",  # Potential seasonal heat storage
    "gas",                       # May be the carrier used for gas stores
    "H2",                        # Hydrogen storage
]



def get_dual_seasonal_storage_value(n, seasonal_storage_type):

    tables = {}

    for component, assets, time_series in [("StorageUnit", n.storage_units, n.storage_units_t),("Store", n.stores, n.stores_t),]:
        
        selected = assets.index[assets["carrier"].isin(seasonal_storage_types)]

        if selected.empty:
            continue

        duals = time_series.get("mu_energy_balance")

        if duals is None:
            raise ValueError(
                f"No storage-balance duals saved for {component}."
            )

        missing = selected.difference(duals.columns)
        if not missing.empty:
            raise ValueError(
                f"Missing duals for {component}: {missing.tolist()}. "
                "Check whether duals were assigned before network export."
            )

        values = duals.loc[:, selected]

        if values.isna().any().any():
            raise ValueError(
                f"Some selected {component} duals contain missing values."
            )

        tables[component] = values

    if not tables:
        raise ValueError("No storage assets matched the selected carriers.")

    return pd.concat(tables, axis=1, names=["component", "asset"])

    
def apply_duals_marginal_cost(n, seasonal_storage_duals):
    for (component, asset), values in seasonal_storage_duals.items():
        if component == "StorageUnit":
            n.storage_units_t.loc[:, asset] = values
        elif component == "Store":
            n.stores_t.loc[:, asset] = values
        else:
            raise ValueError(f"Unknown component: {component}")



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

    add_fuel_backup_links(n, dict(snakemake.params.fuel_backup))
    add_load_shedding(n, **dict(snakemake.params.load_shedding))
    assert_fixed_capacities(n)
    n.meta = {**n.meta, "disruption_preparation": {
        "base_network": str(snakemake.input.net),
        "weather_network": str(weather_file) if weather_file else None,
        "dispatch_only": True,
    }}
    Path(snakemake.output.net).parent.mkdir(parents=True, exist_ok=True)
    n.export_to_netcdf(snakemake.output.net)
