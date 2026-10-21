# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
# SPDX-License-Identifier: MIT
"""Check legacy capacity labels and existing pit ratings after reclustering."""

import unittest

import pandas as pd
import pypsa

from scripts.existing_capacity_nodes import (
    apply_existing_pit_capacities,
    map_existing_capacity_nodes,
)


class ExistingCapacityNodesTests(unittest.TestCase):
    def test_mapping_keeps_capacity_values_and_input_table(self):
        n = pypsa.Network()
        n.add("Bus", "DK0 0AC", carrier="AC")
        n.add("Bus", "DK2 0AC", carrier="AC")
        data = pd.DataFrame({"node": ["DK0 0AC", "DK1 0AC"], "capacity": [10, 20]})
        mapped = map_existing_capacity_nodes(n, data, {"DK1 0AC": "DK2 0AC"})
        self.assertEqual(mapped.node.tolist(), ["DK0 0AC", "DK2 0AC"])
        self.assertEqual(mapped.capacity.tolist(), [10, 20])
        self.assertEqual(data.node.tolist(), ["DK0 0AC", "DK1 0AC"])
        with self.assertRaisesRegex(ValueError, "existing_capacity_node_map"):
            map_existing_capacity_nodes(n, data)

    def test_pit_ratings_use_mapped_equipment_and_output_efficiency(self):
        n = pypsa.Network()
        n.add("Bus", "DK2 0AC", carrier="AC")
        heat, tank = "DK2 0AC urban central heat", "DK2 0AC urban central water pits"
        n.add("Bus", heat)
        n.add("Bus", tank)
        n.add("Store", tank, bus=tank, e_nom_extendable=True)
        n.add("Link", tank + " charger", bus0=heat, bus1=tank, p_nom_extendable=True)
        n.add("Link", tank + " discharger", bus0=tank, bus1=heat,
              efficiency=0.9, p_nom_extendable=True)
        n.links.loc[tank + " charger", "energy to power ratio"] = 10
        apply_existing_pit_capacities(n, {"DK1 0AC": 55000}, {"DK1 0AC": "DK2 0AC"})
        self.assertEqual(n.stores.at[tank, "e_nom"], 55000)
        self.assertFalse(n.stores.at[tank, "e_nom_extendable"])
        self.assertEqual(n.links.at[tank + " charger", "p_nom"], 5500)
        self.assertAlmostEqual(n.links.at[tank + " discharger", "p_nom"] * 0.9, 5500)
        self.assertFalse(n.links.p_nom_extendable.any())
        self.assertNotIn("DK1 0AC urban central water pits", n.stores.index)

    def test_missing_pit_fails_without_creating_empty_equipment(self):
        n = pypsa.Network()
        n.add("Bus", "DK2 0AC", carrier="AC")
        with self.assertRaisesRegex(ValueError, "Missing pit storage equipment"):
            apply_existing_pit_capacities(n, {"DK1 0AC": 55000}, {"DK1 0AC": "DK2 0AC"})
        self.assertTrue(n.stores.empty)


if __name__ == "__main__":
    unittest.main()
