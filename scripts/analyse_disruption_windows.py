"""Select electrical windows for interconnectors and heating windows for fuels."""
from pathlib import Path

import numpy as np
import pandas as pd


TIMING_METRICS = {
    "interconnector": "total_electricity_minus_available_wind_solar_v1",
    "fuel": "useful_heat_minus_available_wind_solar_v1",
}
TIMING_COLUMNS = {
    "interconnector": "electrical_residual_load_MW",
    "fuel": "heat_stress_MW",
}
ELECTRIC_CARRIERS = ["AC", "low voltage"]


def residual_load(n, region, renewable_carriers):
    """Separate electrical residual load and a heat-demand/low-VRE stress proxy.

    Electrical consumption includes heating, conversion and charging, including
    reverse-flow heat pumps. Count final heat Loads once, separately. Transfers
    and thermal storage flows are not additional final demand.
    """
    from scripts.disruption_storage_msv import validate_hourly_network

    validate_hourly_network(n)
    if n.objective is None or not np.isfinite(n.objective):
        raise ValueError("Event timing requires a successfully solved baseline.")
    buses = n.buses.index[n.buses.location.eq(region) | n.buses.index.to_series().eq(region)]
    electrical = buses[n.buses.loc[buses, "carrier"].isin(ELECTRIC_CARRIERS)]
    heat_buses = buses[n.buses.loc[buses, "carrier"].str.endswith(" heat")]
    if electrical.empty:
        raise ValueError(f"No electricity buses for region {region}.")
    loads = n.get_switchable_as_dense("Load", "p_set")

    def demand_at(selected_buses):
        names = n.loads.index[n.loads.bus.isin(selected_buses)]
        values = loads[names].mul(-n.loads.loc[names, "sign"])
        if not np.isfinite(values.to_numpy()).all():
            raise ValueError("Nonfinite final electricity/heat demand.")
        return values.clip(lower=0).sum(axis=1)

    electricity = demand_at(electrical)
    heat = demand_at(heat_buses)
    electricity_to_heat = pd.Series(0.0, index=n.snapshots)
    # Household heat remains on its original final Load. Deliveries, thermal
    # charging, heat losses and heat shedding are not additional heat demand.
    thermal = (n.buses.carrier.str.endswith(" heat")
               | n.buses.carrier.str.endswith(("water tanks", "water pits", "aquifer thermal energy storage")))
    thermal_buses = n.buses.index[thermal]
    b1_electric = n.links.bus1.map(n.buses.carrier).isin(ELECTRIC_CARRIERS)
    # A primary heat output also covers electric auxiliaries on extra ports.
    # Heat recovered as an electrolysis byproduct is not the primary output.
    heat_production = (n.links.bus1.isin(thermal_buses)
                       | (b1_electric & n.links.bus0.isin(thermal_buses))
                       | n.links.carrier.str.contains(r"heat pump|resistive heater|electric boiler", case=False))
    ports = [column for column in n.links if column.startswith("bus") and column[3:].isdigit()]
    has_non_electric = pd.Series(False, index=n.links.index)
    for port in ports:
        endpoints = n.links[port]
        has_non_electric |= endpoints.ne("") & ~endpoints.map(n.buses.carrier).isin(ELECTRIC_CARRIERS)
    for port in ports:
        names = n.links.index[n.links[port].isin(electrical) & has_non_electric]
        if names.empty:
            continue
        # Positive p_i withdraws electricity, including port 1 of reversed HPs.
        # NetCDF can omit all-zero columns; existing NaNs still fail validation.
        flow = n.links_t.get("p" + port[3:], pd.DataFrame(index=n.snapshots))
        power = flow.reindex(index=n.snapshots, columns=names, fill_value=0)
        if not np.isfinite(power.to_numpy()).all():
            raise ValueError("Missing solved electricity-consuming Link flows.")
        withdrawal = power.clip(lower=0)
        electricity += withdrawal.sum(axis=1)
        electricity_to_heat += withdrawal.loc[:, heat_production.loc[names]].sum(axis=1)
    units = n.storage_units.index[n.storage_units.bus.isin(electrical)]
    if len(units):
        charging = n.storage_units_t.p_store.reindex(index=n.snapshots, columns=units, fill_value=0)
        if not np.isfinite(charging.to_numpy()).all():
            raise ValueError("Missing solved StorageUnit charging.")
        electricity += charging.clip(lower=0).sum(axis=1)
    stores = n.stores.index[n.stores.bus.isin(electrical)]
    if len(stores):
        power = n.stores_t.p.reindex(index=n.snapshots, columns=stores, fill_value=0)
        if not np.isfinite(power.to_numpy()).all():
            raise ValueError("Missing solved electrical Store charging.")
        electricity += (-power).clip(lower=0).sum(axis=1)
    generators = n.generators.index[
        n.generators.bus.isin(electrical) & n.generators.carrier.isin(renewable_carriers)]
    if generators.empty:
        raise ValueError("No selected wind/solar generators in the analysis region.")
    profiles = n.get_switchable_as_dense("Generator", "p_max_pu")[generators]
    capacities = n.generators.loc[generators, "p_nom"]
    if not np.isfinite(profiles.to_numpy()).all() or not np.isfinite(capacities).all():
        raise ValueError("Nonfinite renewable availability or capacities.")
    available = profiles.mul(capacities, axis=1).sum(axis=1)
    result = pd.DataFrame({
        "electricity_total_MW": electricity,
        "electricity_to_heat_MW": electricity_to_heat,
        "electricity_excluding_heat_MW": electricity - electricity_to_heat,
        "heat_demand_MW": heat,
        "renewable_available_MW": available,
    })
    result["electrical_residual_load_MW"] = electricity - available
    result["heat_stress_MW"] = heat - available
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


def campaign_windows(hourly, durations):
    """Select each campaign/duration independently from the same baseline."""
    frames = []
    for campaign, column in TIMING_COLUMNS.items():
        windows = select_windows(hourly[column], durations)
        for component in ("electricity_total_MW", "electricity_to_heat_MW", "heat_demand_MW", "renewable_available_MW"):
            windows[component.replace("_MW", "_MWh")] = [
                hourly.loc[(hourly.index >= row.start) & (hourly.index < row.end), component].sum()
                for row in windows.itertuples()]
        windows.insert(0, "timing_metric", TIMING_METRICS[campaign])
        windows.insert(0, "campaign", campaign)
        frames.append(windows)
    return pd.concat(frames, ignore_index=True)


def select_campaign_windows(windows, campaign, durations=None):
    """Reject obsolete/other-family windows rather than silently reusing dates."""
    if campaign not in TIMING_METRICS:
        raise ValueError(f"Unknown timing campaign {campaign!r}.")
    windows = pd.DataFrame(windows)
    if not {"campaign", "timing_metric", "duration_hours"}.issubset(windows.columns):
        raise ValueError("Old timing metric/table layout; rerun timing analysis first.")
    selected = windows.loc[windows.campaign.eq(campaign)].copy()
    if selected.empty or not selected.timing_metric.eq(TIMING_METRICS[campaign]).all():
        raise ValueError(f"Wrong or old timing metric for {campaign}; rerun timing analysis first.")
    selected["duration_hours"] = selected.duration_hours.astype(int)
    if selected.duration_hours.duplicated().any() or selected.duration_hours.le(0).any():
        raise ValueError(f"Duplicate/invalid timing durations for {campaign}.")
    if durations is not None and set(selected.duration_hours) != set(durations):
        raise ValueError(f"Event window durations differ from campaign {campaign}.")
    return selected


if __name__ == "__main__":
    import pypsa

    n = pypsa.Network(snakemake.input.network)
    hourly = residual_load(n, snakemake.params.region, snakemake.params.renewable_carriers)
    windows = campaign_windows(hourly, snakemake.params.durations)
    windows.insert(0, "weather_year", snakemake.params.weather_year)
    windows.insert(0, "variant", snakemake.params.variant)
    Path(snakemake.output.windows).parent.mkdir(parents=True, exist_ok=True)
    hourly.to_csv(snakemake.output.hourly)
    windows.to_csv(snakemake.output.windows, index=False)
