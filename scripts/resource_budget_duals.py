# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
# SPDX-License-Identifier: MIT
"""Export single-period annual fuel-budget duals without changing constraints.

Raw solver signs are retained. An upper-bound dual is generally nonpositive;
its negative is the scarcity premium. Coincident lower/upper limits and nested
budgets require joint interpretation. These are reference-solve marginal values,
not guarantees of availability under different dispatch conditions.
"""

from pathlib import Path

import numpy as np
import pandas as pd

FUEL_CARRIERS = {
    "solid biomass", "biogas", "unsustainable solid biomass",
    "unsustainable biogas", "unsustainable bioliquids",
}
COLUMNS = [
    "constraint", "component", "asset", "carrier", "sense", "rhs_MWh",
    "raw_dual", "annual_min_MWh", "annual_max_MWh",
]


def save_resource_budget_duals(n, csv_path):
    """Save active fuel-limit rows to CSV and JSON-safe network metadata.

    Read Linopy directly: PyPSA does not persist scalar Generator duals.
    Zero allowances are retained; masked/inactive constraint rows are omitted.
    Waste, HVC, imports and their storage-balance duals are not modified.
    """
    if isinstance(n.snapshots, pd.MultiIndex):
        raise ValueError("Fuel-budget dual export currently supports single-period networks only.")
    model_snapshots = n.model.parameters.snapshots.to_index()
    if not model_snapshots.equals(n.snapshots):
        raise ValueError("Annual fuel-budget dual export requires the full snapshot horizon.")
    records = []
    fuels = n.generators.loc[n.generators.carrier.isin(FUEL_CARRIERS)]
    for bound in ("min", "max"):
        name = f"Generator-e_sum_{bound}"
        expected = fuels.index[np.isfinite(fuels[f"e_sum_{bound}"])]
        active = n.get_active_assets("Generator")
        expected = expected.intersection(active.index[active])
        if expected.empty:
            continue
        if name not in n.model.constraints:
            raise ValueError(f"Missing expected fuel-budget constraint: {name}")
        constraint = n.model.constraints[name]
        for asset in expected:
            # The component dimension was renamed between PyPSA versions.
            dimension = next(iter(constraint.labels.dims))
            row = constraint.sel({dimension: asset})
            if row.labels.item() < 0:
                continue
            records.append({
                "constraint": name, "component": "Generator", "asset": asset,
                "carrier": fuels.at[asset, "carrier"], "sense": row.sign.item(),
                "rhs_MWh": float(row.rhs.item()), "raw_dual": float(row.dual.item()),
                "annual_min_MWh": (
                    float(fuels.at[asset, "e_sum_min"])
                    if np.isfinite(fuels.at[asset, "e_sum_min"]) else None
                ),
                "annual_max_MWh": (
                    float(fuels.at[asset, "e_sum_max"])
                    if np.isfinite(fuels.at[asset, "e_sum_max"]) else None
                ),
            })
    budgets = n.global_constraints.loc[
        n.global_constraints.type.eq("operational_limit")
        & n.global_constraints.carrier_attribute.isin(FUEL_CARRIERS)
    ]
    for asset, budget in budgets.iterrows():
        name = f"GlobalConstraint-{asset}"
        if name not in n.model.constraints:
            raise ValueError(f"Missing expected fuel-budget constraint: {name}")
        row = n.model.constraints[name]
        records.append({
            "constraint": name, "component": "GlobalConstraint", "asset": asset,
            "carrier": budget.carrier_attribute, "sense": row.sign.item(),
            "rhs_MWh": float(row.rhs.item()), "raw_dual": float(row.dual.item()),
            "annual_min_MWh": None, "annual_max_MWh": None,
        })
    result = pd.DataFrame(records, columns=COLUMNS)
    if not result.empty and not np.isfinite(result[["rhs_MWh", "raw_dual"]].to_numpy()).all():
        raise ValueError("Cannot export fuel budgets without finite quantities and solved duals.")
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(path, index=False)
    n.meta = {**(n.meta or {}), "resource_budget_duals": records}
    return result
