"""ScarcitySentinelTask / iron_plate_scarcity_sentinel.

Unit checks (no server) for config plumbing plus live checks against the
WRENCH benchmark server (port 27000): the map's natural resource layout
really is scarce near spawn (no task-level patch manipulation needed), and
setup_instance/verify don't crash with the scarcity starting inventory.
"""

import base64
import json
import zlib
from collections import defaultdict

import pytest

from fle.eval.tasks import ScarcitySentinelTask, SCARCITY_STARTING_INVENTORY
from fle.eval.tasks.throughput_task import LAB_PLAY_POPULATED_STARTING_INVENTORY


class TestUnit:
    def test_registers_as_scarcity_task_with_bare_inventory(self):
        from fle.eval.tasks.task_definitions.task_registry import create_task

        task = create_task("iron_plate_scarcity_sentinel")
        assert isinstance(task, ScarcitySentinelTask)
        assert task.starting_inventory == SCARCITY_STARTING_INVENTORY
        # Scarcity variant must not silently inherit the populated kit or
        # the "everything researched" shortcut from ThroughputTask.
        assert task.starting_inventory != LAB_PLAY_POPULATED_STARTING_INVENTORY
        assert task.all_technology_reserached is False
        assert "boiler" not in task.starting_inventory
        assert "steam-engine" not in task.starting_inventory
        assert "electric-mining-drill" not in task.starting_inventory
        assert task.quota == 16
        assert len(task.disruptions) == 2

    def test_goal_explains_scarcity_without_leaking_schedule(self):
        from fle.eval.tasks.task_definitions.task_registry import create_task

        task = create_task("iron_plate_scarcity_sentinel")
        goal = task.goal_description.lower()
        # Must explain the scarcity framing to the agent...
        assert "minimal" in goal or "limited" in goal
        assert "report_fault" in goal
        # ...but never the disruption schedule, kinds-per-task, or seeds.
        assert "seed" not in goal
        assert "delay" not in goal
        assert "entity_destruction" not in goal
        assert "resource_exhaustion" not in goal
        for spec in task.disruptions:
            assert str(spec.seed) not in goal.replace("16", "")

    def test_starting_inventory_is_burner_tier_only(self):
        # Every item in the kit must be a burner/manual-tier item -- no
        # electric-anything, matching the "scarcity" starting-conditions
        # design (see fle/eval/tasks/scarcity_task.py for the full
        # justification and live-calibration notes).
        electric_markers = ("electric", "assembling", "boiler", "steam", "pipe")
        for item in SCARCITY_STARTING_INVENTORY:
            assert not any(marker in item for marker in electric_markers), item


# ---------------------------------------------------------------------------
# Live checks against the WRENCH benchmark server (port 27000)
# ---------------------------------------------------------------------------

RESOURCE_CLUSTER_THRESHOLD = 2.5  # tiles; contiguous ore patches are dense
NEARBY_RADIUS = 150  # tiles from spawn considered "practically reachable"
SEARCH_RADIUS = 800  # tiles from spawn searched for the property overall


def _bulk_lua(instance, lua_expr: str, key: str):
    """Fetch a Lua table as zlib+base64 over a raw rcon.print.

    Mirrors InjectDisruption._bulk_read: bulk data bypasses Tool.execute's
    dump/slpp round-trip, which corrupts hyphenated strings and long lists.
    """
    raw = instance.rcon_client.send_command(
        f"/silent-command rcon.print(helpers.encode_string("
        f"helpers.table_to_json({lua_expr})))"
    )
    decoded = json.loads(zlib.decompress(base64.b64decode(raw))).get(key, [])
    return decoded if isinstance(decoded, list) else []


def _cluster(tiles, threshold=RESOURCE_CLUSTER_THRESHOLD):
    """Union-find clustering of resource tiles into contiguous patches."""
    n = len(tiles)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            dx = tiles[i]["x"] - tiles[j]["x"]
            dy = tiles[i]["y"] - tiles[j]["y"]
            if dx * dx + dy * dy <= threshold * threshold:
                union(i, j)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(tiles[i])
    return list(groups.values())


@pytest.mark.wrench_live
def test_only_one_nearby_iron_ore_patch(instance):
    """Confirms the "scarcity" premise empirically: no task-level patch
    manipulation is used for this task family (see scarcity_task.py) because
    the live map already has exactly one practically-reachable iron-ore
    patch near spawn, with the next-nearest patch far outside a reasonable
    walking detour."""
    expr = (
        "(function() local out = {} "
        f"for _, e in pairs(game.surfaces[1].find_entities_filtered("
        f"{{area = {{{{-{SEARCH_RADIUS}, -{SEARCH_RADIUS}}}, "
        f"{{{SEARCH_RADIUS}, {SEARCH_RADIUS}}}}}, name = 'iron-ore'}})) do "
        "table.insert(out, {x = e.position.x, y = e.position.y}) end "
        "return {tiles = out} end)()"
    )
    tiles = _bulk_lua(instance, expr, "tiles")
    assert tiles, "expected iron-ore somewhere within the search radius"

    patches = _cluster(tiles)
    distances = []
    for patch in patches:
        cx = sum(t["x"] for t in patch) / len(patch)
        cy = sum(t["y"] for t in patch) / len(patch)
        distances.append((cx**2 + cy**2) ** 0.5)
    distances.sort()

    nearby = [d for d in distances if d <= NEARBY_RADIUS]
    assert len(nearby) == 1, (
        f"expected exactly one iron-ore patch within {NEARBY_RADIUS} tiles "
        f"of spawn, found patch centroid distances {distances}"
    )
    # If a second patch exists at all, it must not be a cheap detour --
    # otherwise "only one nearby patch" isn't a real constraint.
    if len(distances) > 1:
        assert distances[1] - distances[0] > NEARBY_RADIUS, (
            "a second iron-ore patch is within easy relocation distance of "
            f"the first: distances={distances}"
        )


@pytest.mark.wrench_live
def test_setup_instance_arms_with_scarcity_inventory(instance, tmp_path):
    """setup_instance must not crash when handed the bare scarcity kit, and
    must arm both chained disruptions exactly like the populated-inventory
    sentinels do."""
    from fle.eval.tasks.task_definitions.task_registry import create_task

    task = create_task("iron_plate_scarcity_sentinel")
    task.ledger_dir = str(tmp_path)
    task.setup(instance)

    assert len(task.engine_ids) == 2
    inv = instance.namespace.inspect_inventory()
    for item, qty in task.starting_inventory.items():
        assert inv.get(item, 0) == qty

    # No electric-tier tech should be pre-unlocked for this task: only
    # whatever the engine's reset.lua bootstraps automatically (automation-
    # science-pack, to break the lab chicken-and-egg -- see scarcity_task.py)
    # should be researched, never "electronics" or "automation".
    researched = _bulk_lua(
        instance,
        "(function() local out = {} "
        "for name, t in pairs(game.forces.player.technologies) do "
        "if t.researched then table.insert(out, name) end end "
        "return {names = out} end)()",
        "names",
    )
    assert "electronics" not in researched
    assert "automation" not in researched
