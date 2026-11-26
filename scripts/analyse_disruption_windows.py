"""Select complete hourly windows with maximum signed DK2 residual energy."""
from pathlib import Path

import numpy as np
import pandas as pd


def residual_load(n, region, renewable_carriers):
    """Gross electrical use, excluding internal distribution and interconnectors.

    Fixed demand plus conversion/charging input; available wind/solar before
    curtailment. Each consumption is counted once, at its electricity input.
    """
    from scripts.disruption_storage_msv import validate_hourly_network

    validate_hourly_network(n)
    if n.objective is None or not np.isfinite(n.objective):
        raise ValueError("Event timing requires a successfully solved baseline.")
    buses = n.buses.index[n.buses.location.eq(region) | n.buses.index.to_series().eq(region)]
    electrical = buses[n.buses.loc[buses, "carrier"].isin(["AC", "low voltage"])]
    if electrical.empty:
        raise ValueError(f"No electricity buses for region {region}.")
    loads = n.loads.index[n.loads.bus.isin(electrical)]
    fixed = n.get_switchable_as_dense("Load", "p_set")[loads]
    if not np.isfinite(fixed.to_numpy()).all():
        raise ValueError("Nonfinite fixed electricity demand.")
    load = fixed.mul(-n.loads.loc[loads, "sign"]).sum(axis=1)
    output_electric = n.links.bus1.map(n.buses.carrier).isin(["AC", "low voltage"])
    links = n.links.index[n.links.bus0.isin(electrical) & ~output_electric]
    # PyPSA may omit all-zero output columns in NetCDF; existing NaNs still fail.
    if len(links):
        p0 = n.links_t.p0.reindex(index=n.snapshots, columns=links, fill_value=0)
        if not np.isfinite(p0.to_numpy()).all():
            raise ValueError("Missing solved electricity-consuming Link flows.")
        load += p0.clip(lower=0).sum(axis=1)
    units = n.storage_units.index[n.storage_units.bus.isin(electrical)]
    if len(units):
        charging = n.storage_units_t.p_store.reindex(index=n.snapshots, columns=units, fill_value=0)
        if not np.isfinite(charging.to_numpy()).all():
            raise ValueError("Missing solved StorageUnit charging.")
        load += charging.clip(lower=0).sum(axis=1)
    stores = n.stores.index[n.stores.bus.isin(electrical)]
    if len(stores):
        power = n.stores_t.p.reindex(index=n.snapshots, columns=stores, fill_value=0)
        if not np.isfinite(power.to_numpy()).all():
            raise ValueError("Missing solved electrical Store charging.")
        load += (-power).clip(lower=0).sum(axis=1)
    generators = n.generators.index[
        n.generators.bus.isin(electrical) & n.generators.carrier.isin(renewable_carriers)]
    if generators.empty:
        raise ValueError("No selected wind/solar generators in the analysis region.")
    profiles = n.get_switchable_as_dense("Generator", "p_max_pu")[generators]
    capacities = n.generators.loc[generators, "p_nom"]
    if not np.isfinite(profiles.to_numpy()).all() or not np.isfinite(capacities).all():
        raise ValueError("Nonfinite renewable availability or capacities.")
    available = profiles.mul(capacities, axis=1).sum(axis=1)
    result = pd.DataFrame({"load_MW": load, "renewable_available_MW": available})
    result["residual_load_MW"] = load - available
    if not np.isfinite(result.to_numpy()).all():
        raise ValueError("Nonfinite residual-load inputs.")
    return result.rename_axis("snapshot")


def select_windows(series, durations):
    """First maximum wins ties; end exclusive; full windows only, no wrap."""
    if (not isinstance(series.index, pd.DatetimeIndex) or series.index.tz is not None
            or series.empty or not series.index.is_unique or not series.index.is_monotonic_increasing
            or not np.isfinite(series.to_numpy()).all()
            or not series.index.to_series().diff().dropna().eq(pd.Timedelta(hours=1)).all()):
        raise ValueError("Expected finite consecutive timezone-naive hourly residual load.")
    if not durations or len(durations) != len(set(durations)):
        raise ValueError("Durations must be nonempty and unique.")
    rows = []
    for hours in durations:
        if isinstance(hours, bool) or not isinstance(hours, int) or not 0 < hours <= len(series):
            raise ValueError(f"Invalid or incomplete duration: {hours}.")
        energy = series.rolling(hours, min_periods=hours).sum()
        end_hour = energy.idxmax()
        start = end_hour - pd.Timedelta(hours=hours - 1)
        rows.append({"duration_hours": hours, "start": start,
                     "end": end_hour + pd.Timedelta(hours=1),
                     "residual_energy_MWh": float(energy.loc[end_hour])})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import pypsa

    n = pypsa.Network(snakemake.input.network)
    hourly = residual_load(n, snakemake.params.region, snakemake.params.renewable_carriers)
    windows = select_windows(hourly.residual_load_MW, snakemake.params.durations)
    windows.insert(0, "weather_year", snakemake.params.weather_year)
    windows.insert(0, "variant", snakemake.params.variant)
    Path(snakemake.output.windows).parent.mkdir(parents=True, exist_ok=True)
    hourly.to_csv(snakemake.output.hourly)
    windows.to_csv(snakemake.output.windows, index=False)
