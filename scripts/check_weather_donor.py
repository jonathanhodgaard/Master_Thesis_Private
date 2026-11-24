"""Reject non-finite donor profiles before starting disruption campaigns."""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa


def validate_donor(network, start, end):
    expected = pd.date_range(start, end, freq="h", inclusive="left")
    expected = expected[~((expected.month == 2) & (expected.day == 29))]
    if not network.snapshots.equals(expected):
        raise ValueError("Donor calendar differs from requested hourly period with leap day removed.")
    attributes = {
        "Load": ["p_set"],
        "Generator": ["p_max_pu", "p_min_pu"],
        "StorageUnit": ["inflow", "p_max_pu", "p_min_pu"],
        "Link": ["p_max_pu", "p_min_pu", "efficiency"],
        "Store": ["e_max_pu", "e_min_pu"],
        "Line": ["s_max_pu"],
    }
    attributes["Link"] += [key for key in network.components["Link"].dynamic
                            if key.startswith("efficiency") and key != "efficiency"]
    problems = []
    for component, attrs in attributes.items():
        table = network.components[component]
        for attr in attrs:
            names = table.static.index if component == "Load" else table.dynamic[attr].columns
            if names.empty:
                continue
            values = network.get_switchable_as_dense(component, attr).reindex(columns=names)
            invalid = ~np.isfinite(values.to_numpy())
            if invalid.any():
                examples = [f"{values.index[i]} / {values.columns[j]}"
                            for i, j in np.argwhere(invalid)[:5]]
                problems.append(f"{component}.{attr}: {invalid.sum()} invalid values; " + "; ".join(examples))
    if problems:
        raise ValueError("Invalid donor profiles:\n" + "\n".join(problems))
    print(f"Validated {len(expected)} hourly snapshots and finite weather/demand profiles.", flush=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("network", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    validate_donor(pypsa.Network(args.network), args.start, args.end)


if __name__ == "__main__":
    main()
