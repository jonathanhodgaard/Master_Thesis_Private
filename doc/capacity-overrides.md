# Capacity overrides during sector preparation

`sector.capacity_overrides` sets capacity floors and expansion flags in
`prepare_sector_network.py`. No additional optimisation constraints are needed.
It defaults to an empty list and leaves existing behaviour unchanged.

```yaml
sector:
  capacity_overrides:
    - name: DE_rooftop
      years: [2025]
      component: Generator
      carrier: solar rooftop
      countries: [DE]
      minimum_mw: 55000
      extendable: true
      distribution: existing_then_potential
```

This illustrative rule establishes at least 55 GW across **all** selected German
rooftop generators, including imported capacity. It does not add another 55 GW
or assign 55 GW to each node. Country totals are allocated before optimisation;
the optimiser cannot relocate these local capacity floors. The actual German
utility/rooftop split must be reconciled before enabling this example: rooftop
capacity already included in imported utility PV must not be counted twice.

## Selection

`component` is required. Specify an exact `carrier` and/or explicit component
`names`. Optional selectors are intersected:

| Setting | Meaning |
| --- | --- |
| `years: [2025]` | Planning years; omitted means every year. |
| `countries: [DE]` | Country of the selected connection bus, following sector-bus locations when needed. Quote `'NO'` in YAML. |
| `nodes: [DK1]` | Geographical AC nodes, including equipment on their sector buses. Uses `existing_capacity_node_map` and automatic Danish aliases. |
| `buses: ["DE0 0 low voltage"]` | Exact bus names in the prepared network. No alias substitution. |
| `names: ["specific generator"]` | Exact component names. |
| `port: bus1` | Connection used for selection. Default `bus` for single-bus components, `bus0` for Links, Lines and Transformers. |

Use separate rules for independent country or node totals. One rule with several
countries/nodes sets **one combined total**, not one total per listed location.
Every named location must match equipment. Cluster-specific node and bus names
must be updated when clustering changes; country rules do not depend on these names.

## Capacity and expansion

| Component | Capacity field | Unit |
| --- | --- | --- |
| Generator, Link, StorageUnit | `minimum_mw` | MW |
| Store | `minimum_mwh` | MWh |
| Line, Transformer | `minimum_mva` | MVA |

At each selected component, the baseline is the larger of nominal capacity and
its existing minimum. Only a shortfall against the requested total is allocated.
A lower requested minimum never retires existing capacity. `extendable: false`
freezes the resulting baseline, which can therefore exceed the requested minimum.
`extendable: true` permits expansion above the floor up to existing capacity
potentials. Omit `extendable` to preserve individual expansion flags.

Supply a minimum and/or `extendable`. A flag-only rule can freeze existing
equipment or enable expansion without increasing the current baseline. A zero
minimum does not remove equipment or block expansion.

`distribution` controls the allocation of the **shortfall**:

- `existing_then_potential` (default): use existing baseline shares where present;
  once those components reach their limits, allocate to remaining potential.
  If no baseline exists, start with remaining potential.
- `potential`: allocate by remaining capacity potential.
- `equal`: allocate equally, redistributing shares when a component hits its limit.

For unlimited potential, the potential method uses equal weights among the
unlimited components. All methods preserve existing local floors. Capacity
potentials are never raised automatically; infeasible targets produce an error.

For Links, the default `capacity_basis: native` uses the model's `p_nom` units,
normally input MW at bus0. To specify CCGT electrical output instead:

```yaml
sector:
  capacity_overrides:
    - name: DK1_CCGT_output
      years: [2025]
      component: Link
      carrier: CCGT
      nodes: [DK1]
      port: bus1
      capacity_basis: output
      minimum_mw: 500
      extendable: false
```

Output capacity is converted using the selected port's static positive efficiency.
Time-varying or reversed Links require native units. Do not apply an ordinary
bus1 efficiency conversion to reversed heat-pump Links.

A storage-energy override does not automatically resize its charger/discharger.
For coupled equipment, configure the related components consistently. Existing
special handling of pit storage remains available and runs before these overrides.

## Ordering and checks

Overrides run after technology creation, heat-bus clustering, adjustments,
existing-infrastructure imports, and legacy `fix_existing_capacity` handling.
Consequently an explicit expansion flag can override a legacy fixing setting.
Rules cannot create missing technologies or buses.

All active rules are checked before changes are applied. Unknown settings,
unmatched selections, invalid units, overlapping component selections, invalid
bounds and insufficient potential fail with the rule name. Use disjoint rules;
country and node rules selecting the same equipment are not order-dependent
exceptions. The preparation log records component counts, before/after totals,
units and expansion settings for every applied rule.
