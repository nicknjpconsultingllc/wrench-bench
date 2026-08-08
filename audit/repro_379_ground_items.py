"""Repro for upstream issue #379: get_entities is blind to neutral-force ground items.

fle/env/tools/agent/get_entities/server.lua queries
    surface.find_entities_filtered{area = area, force = player.force}
Items dropped on the ground are entities of type "item-entity" (prototype
name "item-on-ground") and belong to the NEUTRAL force, so they are
invisible to get_entities().  Meanwhile placement checks still collide with
them, so an agent can be blocked by an object it has no way to observe.

Repro:
  1. Spawn iron-plate stacks on the ground at (3, 3) via raw Lua.
  2. get_entities(position=(3,3), radius=5) -> expected: the ground items
     appear; actual: they are absent.
  3. can_place_entity(StoneFurnace at (3,3)) / place_entity -> document
     whether the invisible items block placement.

PASS = ground items exist server-side but are absent from get_entities().
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, lua, Reporter


def main():
    r = Reporter("#379", "get_entities blind to neutral-force ground items")

    inst = connect(inventory={"stone-furnace": 2})
    try:
        inst.reset()
        ns = inst.namespace

        from fle.env.game_types import Prototype
        from fle.env.entities import Position
        from fle.env import Direction

        # Drop items on the ground inside the 2x2 footprint of a furnace at (3,3)
        out = lua(
            inst,
            'local s = game.surfaces[1] '
            'for _, p in pairs({{x=3.2,y=3.2},{x=2.6,y=2.6},{x=3.4,y=2.8}}) do '
            's.create_entity{name="item-on-ground", position=p, '
            'stack={name="iron-plate", count=7}} end '
            'local found = s.find_entities_filtered{area={{2,2},{4,4}}, type="item-entity"} '
            'local desc = {} '
            'for _, e in pairs(found) do table.insert(desc, e.stack.name.."x"..e.stack.count..'
            '"@("..e.position.x..","..e.position.y..") force="..e.force.name) end '
            "rcon.print(#found .. ' item-entities: ' .. table.concat(desc, '; '))",
        )
        r.note(f"raw Lua ground truth: {out}")
        items_exist = out.startswith("3 item-entities")

        ents = ns.get_entities(position=Position(x=3, y=3), radius=5)
        r.note(f"get_entities(position=(3,3), radius=5) -> {ents!r}")
        ground_items_visible = any(
            "item" in getattr(e, "name", "") for e in ents
        )

        can_place = ns.can_place_entity(
            Prototype.StoneFurnace, Direction.UP, Position(x=3, y=3)
        )
        r.note(f"can_place_entity(stone-furnace at (3,3)) -> {can_place}")

        place_error = None
        placed = None
        try:
            placed = ns.place_entity(
                Prototype.StoneFurnace, Direction.UP, Position(x=3, y=3)
            )
            r.note(f"place_entity(stone-furnace at (3,3)) SUCCEEDED -> {placed}")
        except Exception as e:
            place_error = str(e)
            r.note(f"place_entity(stone-furnace at (3,3)) raised: {place_error}")

        reproduced = items_exist and not ground_items_visible
        blocked = (not can_place) or (place_error is not None)
        r.note(
            f"items exist on server: {items_exist}; visible via get_entities: "
            f"{ground_items_visible}; placement blocked by invisible items: {blocked}"
        )
        r.verdict(
            reproduced,
            "agents cannot observe neutral-force ground items"
            + (
                " that also block placement at that spot"
                if blocked
                else "; placement at that spot was NOT blocked in this run (documented as-is)"
            )
            if reproduced
            else "ground items are now visible via get_entities",
        )
        return 0 if reproduced else 1
    finally:
        inst.cleanup()


if __name__ == "__main__":
    sys.exit(main())
