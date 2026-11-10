import pypsa
import pandas as pd

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
    peak_demand = demand_by_bus.max().clip(lower=0)

    sectors = {
        "electricity": (["AC", "low voltage"], elec_cost),
        "heat": (
            ["urban central heat", "urban decentral heat", "rural heat"],
            heat_cost,
        ),
    }

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
        )


if __name__ == "__main__":

    
    n = pypsa.Network(snakemake.input.net)
    
    # fix capacities of the network
    n.optimize.fix_optimal_capacities()
    
    
    n = add_load_shedding(n)
    
    
    
    
    
    n.export_to_netcdf(snakemake.output.net)