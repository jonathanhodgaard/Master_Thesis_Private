# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
# SPDX-License-Identifier: MIT
"""Resolve explicit legacy bus aliases for existing infrastructure inputs."""

import logging

import numpy as np

logger = logging.getLogger(__name__)


def resolve_capacity_node(n, node, node_map=None):
    """Resolve a data label without assuming cluster numbers remain stable."""
    target = (node_map or {}).get(node, node)
    if target not in n.buses.index or n.buses.at[target, "carrier"] != "AC":
        raise ValueError(
            f"Existing infrastructure node {node!r} resolves to {target!r}, which is "
            "not an AC bus. Check sector.existing_capacity_node_map for this clustering."
        )
    return target


def map_existing_capacity_nodes(n, capacities, node_map=None):
    """Copy an input table and consistently remap all infrastructure rows."""
    capacities = capacities.copy()
    resolved = {node: resolve_capacity_node(n, node, node_map)
                for node in capacities.node.unique()}
    for node, target in resolved.items():
        if node != target:
            logger.info("Map existing infrastructure node %s to %s", node, target)
    capacities["node"] = capacities.node.map(resolved)
    return capacities


def apply_existing_pit_capacities(n, capacities_mwh, node_map=None):
    """Fix pit energy and power ratings after resolving legacy bus names."""
    assignments = []
    for node, energy in capacities_mwh.items():
        target = resolve_capacity_node(n, node, node_map)
        store = f"{target} urban central water pits"
        charger, discharger = store + " charger", store + " discharger"
        if store not in n.stores.index or charger not in n.links.index or discharger not in n.links.index:
            raise ValueError(f"Missing pit storage equipment at {target}; check district-heating/PTES settings.")
        ratio = float(n.links.at[charger, "energy to power ratio"])
        eta = float(n.links.at[discharger, "efficiency"])
        if not np.isfinite([energy, ratio, eta]).all() or energy < 0 or ratio <= 0 or eta <= 0:
            raise ValueError(f"Invalid existing pit size or sizing parameters at {target}.")
        assignments.append((store, charger, discharger, energy, energy / ratio, eta))
    for store, charger, discharger, energy, power, eta in assignments:
        n.stores.loc[store, ["e_nom", "e_nom_min", "e_nom_extendable"]] = [energy, energy, False]
        for link, rating in ((charger, power), (discharger, power / eta)):
            n.links.loc[link, ["p_nom", "p_nom_min", "p_nom_extendable"]] = [rating, rating, False]
