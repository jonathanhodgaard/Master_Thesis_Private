# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
# SPDX-License-Identifier: MIT
"""Exercise preparation-only overrides without optional geospatial dependencies."""

import ast
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from scripts.existing_capacity_nodes import resolve_capacity_node


# Import just the preparation functions: the complete script also imports the
# data-building/geospatial stack, which these DataFrame operations do not need.
source = Path(__file__).resolve().parents[1] / "scripts" / "prepare_sector_network.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {"apply_capacity_overrides", "_capacity_override_geography",
                               "_allocate_capacity_override"}]
namespace = dict(np=np, pd=pd, logger=logging.getLogger(__name__),
                 resolve_capacity_node=resolve_capacity_node)
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
apply_capacity_overrides = namespace["apply_capacity_overrides"]


def example():
    buses = pd.DataFrame(
        {"carrier": ["AC", "AC", "AC", "low voltage", "low voltage", "low voltage", "gas"],
         "country": ["DE", "DE", "DK", "", "", "", ""],
         "location": ["", "", "", "DE-A", "DE-B", "DK-A", ""]},
        index=["DE-A", "DE-B", "DK-A", "DE-A lv", "DE-B lv", "DK-A lv", "EU gas"],
    )
    generators = pd.DataFrame(
        {"bus": ["DE-A lv", "DE-B lv", "DK-A lv", "DE-A"],
         "carrier": ["solar rooftop", "solar rooftop", "solar rooftop", "solar"],
         "p_nom": [10000., 5000., 1000., 20000.],
         "p_nom_min": [10000., 5000., 1000., 20000.],
         "p_nom_max": [30000., 50000., 2000., 100000.],
         "p_nom_extendable": [True, True, True, False]},
        index=["roof A", "roof B", "roof DK", "utility"],
    )
    links = pd.DataFrame(
        {"bus0": ["EU gas"], "bus1": ["DE-A"], "carrier": ["CCGT"],
         "p_nom": [100.], "p_nom_min": [100.], "p_nom_max": [2000.],
         "p_nom_extendable": [True], "efficiency": [0.5]}, index=["CCGT A"],
    )
    stores = pd.DataFrame(
        {"bus": ["DE-A lv"], "carrier": ["battery"], "e_nom": [100.],
         "e_nom_min": [200.], "e_nom_max": [1000.], "e_nom_extendable": [True]},
        index=["battery A"],
    )
    return SimpleNamespace(buses=buses, generators=generators, links=links, stores=stores,
                           links_t={"efficiency": pd.DataFrame()})


def rule(**kwargs):
    return dict(component="Generator", carrier="solar rooftop", countries=["DE"],
                minimum_mw=55000, **kwargs)


class CapacityOverrideTests(unittest.TestCase):
    def test_country_total_and_potential_redistribution(self):
        n = example()
        untouched = n.generators.loc[["roof DK", "utility"]].copy()
        apply_capacity_overrides(n, [rule(extendable=True)], 2025)
        self.assertAlmostEqual(n.generators.loc[["roof A", "roof B"], "p_nom_min"].sum(), 55000)
        self.assertAlmostEqual(n.generators.at["roof A", "p_nom_min"], 30000)
        self.assertAlmostEqual(n.generators.at["roof B", "p_nom_min"], 25000)
        pd.testing.assert_frame_equal(n.generators.loc[untouched.index], untouched)

    def test_freezing_and_lower_minimum_preserve_baseline(self):
        n = example()
        r = rule(extendable=False)
        r["minimum_mw"] = 100
        apply_capacity_overrides(n, [r], 2025)
        self.assertEqual(n.generators.loc[["roof A", "roof B"], "p_nom"].sum(), 15000)
        self.assertFalse(n.generators.loc[["roof A", "roof B"], "p_nom_extendable"].any())

    def test_node_alias_reaches_low_voltage(self):
        n = example()
        r = dict(component="Generator", carrier="solar rooftop", nodes=["DK1"],
                 minimum_mw=1200, extendable=False)
        apply_capacity_overrides(n, [r], 2025, {"DK1": "DK-A"})
        self.assertEqual(n.generators.at["roof DK", "p_nom"], 1200)
        self.assertFalse(n.generators.at["roof DK", "p_nom_extendable"])

    def test_link_output_and_port_selection(self):
        n = example()
        r = dict(component="Link", carrier="CCGT", countries=["DE"], port="bus1",
                 capacity_basis="output", minimum_mw=500, extendable=False)
        apply_capacity_overrides(n, [r], 2025)
        self.assertEqual(n.links.at["CCGT A", "p_nom"], 1000)
        self.assertEqual(n.links.at["CCGT A", "p_nom_min"] * 0.5, 500)
        n.links_t["efficiency"] = pd.DataFrame({"CCGT A": [0.5, 0.6]})
        with self.assertRaisesRegex(ValueError, "static positive efficiency"):
            apply_capacity_overrides(n, [r], 2025)
        n.links_t["efficiency"] = pd.DataFrame()
        n.links["p_max_pu"] = 0.0
        with self.assertRaisesRegex(ValueError, "reversed Links"):
            apply_capacity_overrides(n, [r], 2025)

    def test_store_floor_and_exact_names(self):
        n = example()
        apply_capacity_overrides(n, [dict(component="Store", names=["battery A"],
                                         minimum_mwh=500, extendable=False)], 2025)
        self.assertEqual(n.stores.at["battery A", "e_nom"], 500)
        self.assertEqual(n.stores.at["battery A", "e_nom_min"], 500)
        self.assertFalse(n.stores.at["battery A", "e_nom_extendable"])

    def test_year_and_empty_config_leave_network_unchanged(self):
        n = example()
        before = n.generators.copy()
        apply_capacity_overrides(n, [], 2025)
        apply_capacity_overrides(n, [rule(years=[2035])], 2025)
        pd.testing.assert_frame_equal(n.generators, before)

    def test_impossible_target_and_overlap_are_atomic(self):
        for second in [rule(), dict(component="Store", names=["battery A"], minimum_mwh=2000)]:
            n = example()
            before = n.generators.copy()
            with self.assertRaises(ValueError):
                apply_capacity_overrides(n, [rule(), second], 2025)
            pd.testing.assert_frame_equal(n.generators, before)

    def test_unknown_geography_and_invalid_settings_fail(self):
        bad_rules = [dict(countries=["DE", "FR"]), dict(distribution="typo"),
                     dict(extendable="false"), dict(minimum_mw=-1),
                     dict(minimum_mw=float("nan")), dict(minimum_mw=None),
                     dict(minimum_mwh=1), dict(unknown_setting=1),
                     dict(names=["missing"]), dict(buses=["missing"])]
        for changes in bad_rules:
            with self.subTest(changes=changes):
                r = rule()
                r.update(changes)
                with self.assertRaises(ValueError):
                    apply_capacity_overrides(example(), [r], 2025)

    def test_no_baseline_uses_potential_and_handles_unlimited_capacity(self):
        for maximum in ([30000., 50000.], [np.inf, np.inf], [10000., np.inf]):
            n = example()
            idx = ["roof A", "roof B"]
            n.generators.loc[idx, ["p_nom", "p_nom_min"]] = 0.
            n.generators.loc[idx, "p_nom_max"] = maximum
            apply_capacity_overrides(n, [rule()], 2025)
            self.assertAlmostEqual(n.generators.loc[idx, "p_nom"].sum(), 55000)
            self.assertTrue(np.isfinite(n.generators.loc[idx, "p_nom"]).all())

    def test_flag_only_freezes_at_existing_floor(self):
        n = example()
        r = rule(extendable=False)
        del r["minimum_mw"]
        n.generators.at["roof A", "p_nom_min"] = 15000
        apply_capacity_overrides(n, [r], 2025)
        self.assertEqual(n.generators.at["roof A", "p_nom"], 15000)

    def test_equal_potential_and_preserved_expansion(self):
        for distribution in ["equal", "potential"]:
            n = example()
            n.generators.at["roof A", "p_nom_extendable"] = False
            apply_capacity_overrides(n, [rule(distribution=distribution)], 2025)
            self.assertFalse(n.generators.at["roof A", "p_nom_extendable"])
            self.assertTrue(n.generators.at["roof B", "p_nom_extendable"])
            self.assertAlmostEqual(n.generators.loc[["roof A", "roof B"], "p_nom"].sum(), 55000)

    def test_mva_and_storage_power_units(self):
        for component, table, attr, unit in [
            ("Line", "lines", "s_nom", "minimum_mva"),
            ("Transformer", "transformers", "s_nom", "minimum_mva"),
            ("StorageUnit", "storage_units", "p_nom", "minimum_mw"),
        ]:
            n = example()
            data = {"carrier": ["test"], attr: [10.], attr + "_min": [10.],
                    attr + "_max": [100.], attr + "_extendable": [True]}
            data.update({"bus": ["DE-A"]} if component == "StorageUnit"
                        else {"bus0": ["DE-A"], "bus1": ["DE-B"]})
            setattr(n, table, pd.DataFrame(data, index=["equipment"]))
            apply_capacity_overrides(n, [dict(component=component, carrier="test",
                                             extendable=False, **{unit: 50})], 2025)
            self.assertEqual(getattr(n, table).at["equipment", attr], 50)


if __name__ == "__main__":
    unittest.main()
