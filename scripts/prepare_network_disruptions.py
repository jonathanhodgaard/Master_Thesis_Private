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


if __name__ == "__main__":

    
    n = pypsa.Network(snakemake.input.net)
    
    seasonal_storage_duals = get_dual_seasonal_storage_value(n, seasonal_storage_types)
    
    
    
    
    
    
    
    
    n.export_to_netcdf(snakemake.output.net)