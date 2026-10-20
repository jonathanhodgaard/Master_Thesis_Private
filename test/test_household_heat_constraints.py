# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
# SPDX-License-Identifier: MIT
"""Small solved examples checking allocation, flexible tanks and dispatch reuse."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from scripts.household_heat_constraints import (
    META_KEY, SHARE_VARIABLE, add_household_heat_constraints,
    freeze_household_heat_network, prepare_household_heat_network,
    save_household_heat_shares,
    normalise_household_share_table,
)
from scripts.prepare_sector_network import cluster_heat_buses
from scripts.solve_network import add_lossy_bidirectional_link_constraints


def example():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range("2025-01-01", periods=4, freq="h"))
    prefix = "XX residential rural"
    heat, tank = prefix + " heat", prefix + " water tanks"
    for carrier in ["electricity", "gas", "residential rural heat", "residential rural water tanks"]:
        n.add("Carrier", carrier)
    n.add("Bus", "electricity", carrier="electricity")
    n.add("Bus", "gas", carrier="gas")
    n.add("Bus", heat, carrier="residential rural heat", location="XX")
    n.add("Bus", tank, carrier="residential rural water tanks")
    n.add("Load", heat, bus=heat, p_set=[8, 12, 8, 12])
    n.add("Generator", "grid", bus="electricity", p_nom=100, marginal_cost=[5, 200, 5, 200])
    n.add("Generator", "gas supply", bus="gas", p_nom=100, marginal_cost=30)
    n.add("Link", prefix + " air heat pump", bus0=heat, bus1="electricity",
          carrier=prefix.removeprefix("XX ") + " air heat pump",
          efficiency=[1/3, 1/2, 1/3, 1/2], p_min_pu=-1, p_max_pu=0,
          p_nom_extendable=True, capital_cost=20)
    n.add("Link", prefix + " gas boiler", bus0="gas", bus1=heat,
          carrier="residential rural gas boiler", efficiency=0.9,
          p_nom_extendable=True, capital_cost=3)
    n.add("Store", tank, bus=tank, e_nom_extendable=True,
          e_cyclic=True, standing_loss=0.01, capital_cost=1)
    n.add("Link", tank + " charger", bus0=heat, bus1=tank,
          efficiency=0.95, p_nom_extendable=True, capital_cost=0.2)
    n.add("Link", tank + " discharger", bus0=tank, bus1=heat,
          efficiency=0.9, p_nom_extendable=True, capital_cost=0.2)
    n.links.loc[tank + " charger", "energy to power ratio"] = 2.0
    config = {"sector": {META_KEY: {"enable": True}}}
    return n, config


def create(n):
    n.optimize.create_model()
    add_household_heat_constraints(n, n.snapshots)


def merged_example(preserve=True):
    n, config = example()
    n.add("Carrier", "services rural heat")
    n.add("Bus", "XX services rural heat", carrier="services rural heat")
    n.add("Load", "XX services rural heat", bus="XX services rural heat", p_set=10)
    n.add("Load", "XX agriculture heat", bus="XX services rural heat", p_set=3)
    cluster_heat_buses(n, preserve_household_loads=preserve)
    config["sector"]["cluster_heat_buses"] = True
    return n, config


class HouseholdHeatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.shares_path = Path(directory.name) / "household_heat_shares.csv"

    def test_share_roundoff_is_corrected_but_invalid_allocations_rejected(self):
        raw = pd.DataFrame({"heat_bus": ["A", "A"], "share": [0.4, 0.599991]})
        clean = normalise_household_share_table(raw)
        self.assertAlmostEqual(clean.share.sum(), 1, places=14)
        np.testing.assert_allclose(clean.raw_share, raw.share)
        raw.loc[1, "share"] = 0.5
        with self.assertRaisesRegex(ValueError, "sum to one"):
            normalise_household_share_table(raw)

    def test_cost_data_duration_and_existing_heater_capacities(self):
        n, config = example()
        n.links.loc["XX residential rural water tanks charger", "energy to power ratio"] = 0.15
        hp, boiler = "XX residential rural air heat pump", "XX residential rural gas boiler"
        n.links.loc[hp, ["p_nom", "p_nom_min", "p_nom_extendable"]] = [12, 12, False]
        n.links.loc[boiler, ["p_nom", "p_nom_min"]] = [4, 4]
        before = n.links.loc[[hp, boiler], ["p_nom", "p_nom_min", "p_nom_extendable"]].copy()
        prepare_household_heat_network(n, config)
        pd.testing.assert_frame_equal(
            n.links.loc[[hp, boiler], before.columns], before,
        )
        for group in n.meta[META_KEY]["groups"]:
            self.assertAlmostEqual(group["tank_energy_to_power_ratio"], 0.15)
            self.assertAlmostEqual(n.links.at[group["tank"] + " charger", "energy to power ratio"], 0.15)
        create(n)
        self.assertEqual(n.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))
        self.assertAlmostEqual(n.links.at[hp, "p_nom_opt"], 12)
        self.assertGreaterEqual(n.links.at[boiler, "p_nom_opt"], 4 - 1e-6)
        result = save_household_heat_shares(n, self.shares_path)
        shares = result.set_index("technology").share
        self.assertAlmostEqual(shares["air heat pump"], 12 / (12 + 4 * 0.9))
        self.assertAlmostEqual(shares["gas boiler"], 4 * 0.9 / (12 + 4 * 0.9))
        np.testing.assert_allclose(result.tank_capacity_MWh,
                                   result.charger_input_capacity_MW * 0.15, atol=1e-6)

    def test_reversed_flags_and_bidirectional_capacity_constraints(self):
        n, config = example()
        n.add("Link", "transport", bus0="gas", bus1="electricity",
              p_nom_extendable=True, capital_cost=1)
        n.add("Link", "transport-reversed", bus0="electricity", bus1="gas",
              p_nom_extendable=True, capital_cost=1)
        n.links["reversed"] = False
        n.links.at["transport-reversed", "reversed"] = True
        prepare_household_heat_network(n, config)
        self.assertFalse(n.links.reversed.isna().any())
        self.assertTrue(n.links.at["transport-reversed", "reversed"])
        self.assertEqual(n.links.reversed.sum(), 1)
        create(n)
        # Also exercise a missing custom flag from another external component.
        n.links["reversed"] = n.links.reversed.astype(object)
        delivery = n.meta[META_KEY]["groups"][0]["delivery"]
        n.links.at[delivery, "reversed"] = None
        add_lossy_bidirectional_link_constraints(n)
        self.assertIn("Link-bidirectional_sync", n.model.constraints)
        self.assertEqual(n.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))

    def test_joint_investment_and_share_export(self):
        n, config = example()
        prepare_household_heat_network(n, config)
        create(n)
        status, condition = n.optimize.solve_model(solver_name="highs")
        self.assertEqual((status, condition), ("ok", "optimal"))
        result = save_household_heat_shares(n, self.shares_path)
        self.assertNotIn("shares", n.meta[META_KEY])
        self.assertAlmostEqual(result.share.sum(), 1)
        np.testing.assert_allclose(result.tank_capacity_MWh,
                                   2 * result.charger_input_capacity_MW, atol=1e-6)
        np.testing.assert_allclose(result.charger_input_capacity_MW,
                                   result.discharger_heat_capacity_MW, atol=1e-6)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base.nc"
            n.export_to_netcdf(path)
            loaded = pypsa.Network(path)
        self.assertNotIn("shares", loaded.meta[META_KEY])
        freeze_household_heat_network(loaded, self.shares_path)
        create(loaded)
        self.assertEqual(loaded.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))
        reused = save_household_heat_shares(loaded, self.shares_path)
        np.testing.assert_allclose(reused.share, result.share, atol=1e-6)
        self.assertFalse(loaded.links.p_nom_extendable.any())
        self.assertFalse(loaded.stores.e_nom_extendable.any())

    def test_storage_investment_is_optional_and_responds_to_cost(self):
        capacities = []
        for cost in (1, 1e6):
            n, config = example()
            n.stores.at["XX residential rural water tanks", "capital_cost"] = cost
            prepare_household_heat_network(n, config)
            create(n)
            n.model.add_constraints(n.model[SHARE_VARIABLE] == 0.5, name="test-equal-shares")
            self.assertEqual(n.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))
            capacities.append(n.stores.e_nom_opt.sum())
        self.assertGreater(capacities[0], 0.1)
        self.assertAlmostEqual(capacities[1], 0, places=6)

    def test_merged_demand_and_extendable_gas(self):
        n, config = merged_example()
        self.assertNotIn("XX residential rural heat", n.buses.index)
        self.assertNotIn("XX services rural heat", n.buses.index)
        hp, gas = "XX rural air heat pump", "XX rural gas boiler"
        n.links.loc[hp, ["p_nom", "p_nom_min", "p_nom_extendable"]] = [12, 12, False]
        n.links.loc[gas, ["p_nom", "p_nom_min"]] = [4, 4]
        prepare_household_heat_network(n, config)
        create(n)
        self.assertEqual(n.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))
        result = save_household_heat_shares(n, self.shares_path)
        hp_share = 12 / (12 + 4 * 0.9)
        for group in n.meta[META_KEY]["groups"]:
            self.assertTrue(group["merged"])
            share = hp_share if group["technology"] == "air heat pump" else 1 - hp_share
            expected = share * np.array([8, 12, 8, 12])
            if group["technology"] == "gas boiler":
                expected += 13
            np.testing.assert_allclose(n.links_t.p0[group["delivery"]], expected, atol=1e-6)
        self.assertAlmostEqual(n.links.at[hp, "p_nom_opt"], 12)
        self.assertTrue(n.links.at[gas, "p_nom_extendable"])
        self.assertGreater(n.links.at[gas, "p_nom_opt"], 4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merged.nc"
            n.export_to_netcdf(path)
            loaded = pypsa.Network(path)
        freeze_household_heat_network(loaded, self.shares_path)
        create(loaded)
        self.assertEqual(loaded.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))
        reused = save_household_heat_shares(loaded, self.shares_path)
        np.testing.assert_allclose(reused.share, result.share, atol=1e-6)

    def test_merged_shares_optimised_without_existing_data(self):
        n, config = merged_example()
        prepare_household_heat_network(n, config)
        self.assertTrue(all(g["fixed_share"] is None for g in n.meta[META_KEY]["groups"]))
        create(n)
        self.assertEqual(n.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))
        result = save_household_heat_shares(n, self.shares_path)
        self.assertAlmostEqual(result.share.sum(), 1)
        gas = next(g for g in n.meta[META_KEY]["groups"] if g["technology"] == "gas boiler")
        gas_share = result.set_index("technology").at["gas boiler", "share"]
        np.testing.assert_allclose(n.links_t.p0[gas["delivery"]],
                                   gas_share * np.array([8, 12, 8, 12]) + 13, atol=1e-6)

    def test_group_balances_and_gas_outage(self):
        n, config = example()
        prepare_household_heat_network(n, config)
        create(n)
        n.model.add_constraints(n.model[SHARE_VARIABLE] == 0.5, name="test-equal-shares")
        self.assertEqual(n.optimize.solve_model(solver_name="highs"), ("ok", "optimal"))
        save_household_heat_shares(n, self.shares_path)
        groups = n.meta[META_KEY]["groups"]
        for group in groups:
            heater = group["heater"]
            q = -n.links_t.p0[heater] if group["technology"].endswith("heat pump") else -n.links_t.p1[heater]
            charging = n.links_t.p0[group["tank"] + " charger"]
            discharging = -n.links_t.p1[group["tank"] + " discharger"]
            np.testing.assert_allclose(q + discharging - charging, [4, 6, 4, 6], atol=1e-6)
        hp = next(g for g in groups if g["technology"] == "air heat pump")
        self.assertGreater(n.links_t.p0[hp["tank"] + " charger"].max(), 0.1)
        freeze_household_heat_network(n, self.shares_path)
        n.generators.at["gas supply", "p_max_pu"] = 0
        create(n)
        # Heat pumps cannot supply gas households through a common tank.
        self.assertEqual(n.optimize.solve_model(solver_name="highs")[1], "infeasible")

    def test_old_merged_and_unallocated_equipment_rejected(self):
        n, config = merged_example(preserve=False)
        with self.assertRaisesRegex(ValueError, "preserved residential demand"):
            prepare_household_heat_network(n, config)
        n, config = example()
        n.add("Generator", "unallocated heat", bus="XX residential rural heat", p_nom=10)
        with self.assertRaisesRegex(ValueError, "Unallocated Generators"):
            prepare_household_heat_network(n, config)
        self.assertNotIn(META_KEY, n.meta)


if __name__ == "__main__":
    unittest.main()
