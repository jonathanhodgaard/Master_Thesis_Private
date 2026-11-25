# SPDX-License-Identifier: MIT
"""Shedding spread compared between networks: 2025, 2035 with V2G, 2035 without V2G.

Standalone (not part of the Snakemake workflow). Same layout as the shedding spread plot
(scripts/plot_disruption_spread.py): x = interconnector cases, three panels (peak load
shedding MW, energy not served GWh, hours with load shedding), DK2, full simulated year.
Per case one grey min-max band over ALL solved values (up to 5 weather years x 3
networks = 15), dots coloured by network only (weather years are not told apart: the
spread is what matters). The networks' dots sit side by side inside the band.

Reads the spread tables the spread script writes per network, so run that first for
every network (from the repository root)::

    python scripts/plot_disruption_spread.py
    python scripts/plot_disruption_spread.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_w_V2G.yaml
    python scripts/plot_disruption_spread.py config/config.interconnector_disruption.yaml config/config.interconnector_disruption_2035_wo_V2G.yaml
    python scripts/plot_spread_comparison.py

(on a PC: pixi run python ...). A network without a spread table is skipped with a note.
Output: results/disruptions/comparison/plots/spread_comparison/
    shedding_spread_comparison_<label>.png and .csv
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.plot_disruption_spread import BAND_HALF, CONFIG, PANELS, load_config, x_positions  # noqa: E402

# network name -> (colour, config files merged in order); colours Okabe-Ito
NETWORKS = {
    "2025": ("#0072B2", [CONFIG]),
    "2035 with V2G": ("#D55E00", [CONFIG, "config/config.interconnector_disruption_2035_w_V2G.yaml"]),
    "2035 without V2G": ("#009E73", [CONFIG, "config/config.interconnector_disruption_2035_wo_V2G.yaml"]),
}
OUT_DIR = Path("results/disruptions/comparison/plots/spread_comparison")
DOT_STEP = 0.18   # x distance between the networks' dots inside a case


def load_tables():
    # -> (long table: network, weather_year, case, metrics; cases, labels, years, label)
    tables, cases, labels, years, label = [], None, {}, [], "DK2"
    for name, (_, configs) in NETWORKS.items():
        cfg = load_config(configs)["disruption"]
        opts = cfg.get("plotting", {})
        label = opts.get("region_label", label)
        cases = cases or list(cfg["cases"])
        labels = labels or (opts.get("case_labels") or {})
        years = years or list(cfg["weather_years"])
        path = Path(cfg["output_directory"]) / "plots" / "spread" / f"shedding_spread_{label}.csv"
        if not path.exists():
            print(f"{name}: no spread table {path}, skipped "
                  f"(run scripts/plot_disruption_spread.py with {' '.join(configs)})")
            continue
        t = pd.read_csv(path)
        print(f"{name}: {len(t)} solved cells from {path}")
        tables.append(t.assign(network=name))
    if not tables:
        sys.exit("no spread tables found")
    return pd.concat(tables, ignore_index=True), cases, labels, years, label


def plot(table, cases, labels, n_years):
    pos = x_positions(cases)
    names = list(NETWORKS)
    offset = {n: (i - (len(names) - 1) / 2) * DOT_STEP for i, n in enumerate(names)}
    fig, axes = plt.subplots(len(PANELS), 1, figsize=(10, 8.5), sharex=True,
                             constrained_layout=True)
    for ax, (col, ylabel) in zip(axes, PANELS):
        for case in cases:
            vals = table.loc[table.case == case, col]
            if len(vals) > 1:  # min-max band over all networks and weather years
                ax.add_patch(plt.Rectangle((pos[case] - BAND_HALF, vals.min()), 2 * BAND_HALF,
                                           vals.max() - vals.min(), color="#e0e0e0", lw=0,
                                           zorder=1))
        for name in names:
            sub = table[table.network == name]
            if sub.empty:
                continue
            ax.scatter([pos[c] + offset[name] for c in sub.case], sub[col], s=34,
                       color=NETWORKS[name][0], edgecolor="white", lw=0.6, alpha=0.9,
                       zorder=3, clip_on=False, label=name)
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.margins(y=0.12)
        ax.grid(axis="y", color="#e0e0e0", lw=0.6)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        xs = sorted(pos.values())
        for a, b in zip(xs, xs[1:]):
            if b - a > 1:
                ax.axvline((a + b) / 2, color="#9e9e9e", lw=0.8, ls=":")

    solved = table.groupby("case").size()
    total = n_years * len(names)
    ticks = [f"{labels.get(c, c).replace(' + ', ' +' + chr(10)).replace(' (', chr(10) + '(')}"
             f"\n[{solved.get(c, 0)}/{total}]" for c in cases]
    axes[-1].set_xticks([pos[c] for c in cases], ticks, fontsize=8.5)
    axes[-1].set_xlim(min(pos.values()) - 0.6, max(pos.values()) + 0.6)
    handles, shown = axes[0].get_legend_handles_labels()
    for name in names:  # networks without data still listed
        if name not in shown:
            handles.append(plt.Line2D([], [], marker="o", ls="", color=NETWORKS[name][0],
                                      alpha=0.35))
            shown.append(f"{name} (no data)")
    fig.legend(handles, shown, loc="outside upper center", ncol=len(names), frameon=False,
               fontsize=9, title=f"network (dots: weather years; [n] = solved of {total})",
               title_fontsize=9)
    return fig


def main():
    table, cases, labels, years, label = load_tables()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = OUT_DIR / f"shedding_spread_comparison_{label}"
    cols = ["network", "weather_year", "case"] + [c for c, _ in PANELS]
    table[cols].round(3).to_csv(f"{stem}.csv", index=False)
    fig = plot(table, cases, labels, len(years))
    fig.savefig(f"{stem}.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {stem}.png and {stem}.csv")


if __name__ == "__main__":
    main()
