# Hourly MSV and period waste-treatment trial

Load `config/config.disruption_dispatch_2025_hourly_msv.yaml` after the existing
2025 dispatch configuration. Submit on Sophia with:

```bash
sbatch run_dispatch_2025_base_weather_hourly_msv.sbatch
```

This uses the existing five-day optimization horizon and one-day overlap.
Results go to `results/disruptions/sector_2025_rolling_hourly_msv/base_weather`,
preserving the original annual-mean results. Resource allowances remain in
price-only mode. No capacity optimization is scheduled by this batch file.

## Waste treatment

`waste_hvc.treatment_period_hours: 96` divides the reference calendar into fixed
four-day blocks, anchored at its first snapshot. Constant material arrivals can
be stored after arrival, but the HVC backlog must be zero at every block's final
snapshot. The final partial calendar block also has a deadline. Set the option
to null or omit it to retain the previous annual-end deadline behavior.

`backlog_hours: 96` sizes the buffer to 96 hours of combined HVC and municipal
waste arrivals. It controls storage capacity separately from the treatment
deadline. Treatment can use energy recovery or the existing HVC-to-air disposal
route. Energy recovery capacities, conversion efficiencies and heat vents are
unchanged. Accounting disposal is sized to accept current arrivals plus the
whole permitted backlog in one hour, as in the existing implementation.

Deadlines are attached to calendar snapshots, not optimization windows.
Only implemented states are carried forward; overlap results are reoptimized.
A simulation starting inside a block starts with the configured initial
backlog and keeps that block's original deadline. An early simulation cutoff
does not add a new empty-backlog requirement.

## Storage values

`storage.msv.aggregation` accepts `annual_mean` (the existing default) or `hourly`.
Hourly mode uses each selected asset's reference `mu_energy_balance` at the
same snapshot. Asset selection is unchanged, including fuel inventories.
The reference must provide finite duals for all selected assets and hours.

The values enter the existing signed Store marginal-cost mechanism. StorageUnit
discharge bids, charging credits and spill costs retain their efficiency
adjustments and now use the matching hour's value. No separate terminal credit
or additional standing-loss valuation is added. Physical standing-loss handling
and initial-state handover remain unchanged.

`storage_msv.csv` reports each asset's annual mean, aggregation and applied
minimum/maximum. `storage_msv_timeseries.csv` records the actual hourly values
over implemented snapshots, with `Component::asset` columns in EUR/MWh stored.
The existing window summaries account for these hourly MSV charges.

Hourly reference values contain information from the solved reference year;
this is a policy comparison, not an independently forecast storage value.
This trial changes both waste flexibility and the MSV profile. To isolate their
effects, rerun with a separate output directory and `aggregation: annual_mean`
while keeping the new waste settings.
