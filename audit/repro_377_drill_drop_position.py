"""Repro for upstream issue #377: burner-mining-drill drop_position is wrong.

fle/env/mods/serialize.lua (mining-drill branch) rounds the engine's real
drop_position to the nearest 0.5:
    serialized.drop_position.x = math.round(x * 2) / 2
The engine's real drop offset for a burner-mining-drill is 0.796875 tiles
beyond the collision box, so rounding shifts the reported point.  Worse, the
reported point sits so close to the drill that placing the canonical
"furnace at the drill's drop position" fails with a collision error.

Repro:
  1. Place a burner-mining-drill on the nearest iron ore (facing UP).
  2. Compare Python entity .drop_position with the engine's
     LuaEntity.drop_position read over raw RCON.
  3. Try to place a stone-furnace at the reported drop_position.
     Expected: works (this is the tutorial-level use of drop_position);
     Actual: "something is in the way"-style collision error.

PASS = reported value differs from engine value AND/OR furnace placement at
the reported drop_position fails with a collision error.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, lua, Reporter


def main():
    r = Reporter("#377", "drill drop_position wrong / unusable")

    inst = connect(inventory={"burner-mining-drill": 1, "stone-furnace": 1, "coal": 10})
    try:
        inst.reset()
        ns = inst.namespace

        from fle.env.game_types import Prototype, Resource
        from fle.env import Direction

        ore = ns.nearest(Resource.IronOre)
        ns.move_to(ore)
        drill = ns.place_entity(Prototype.BurnerMiningDrill, Direction.UP, ore)
        r.note(f"drill placed at {drill.position}, direction UP")

        py_drop = drill.drop_position
        r.note(f"Python entity drop_position: ({py_drop.x}, {py_drop.y})")

        engine = lua(
            inst,
            'local d = game.surfaces[1].find_entities_filtered{name="burner-mining-drill"}[1] '
            "rcon.print(d.position.x..','..d.position.y..'|'..d.drop_position.x..','..d.drop_position.y)",
        )
        r.note(f"engine says position|drop_position: {engine}")
        pos_part, drop_part = engine.split("|")
        ex, ey = (float(v) for v in drop_part.split(","))
        mismatch = (abs(ex - py_drop.x) > 1e-6) or (abs(ey - py_drop.y) > 1e-6)
        r.note(
            f"reported ({py_drop.x}, {py_drop.y}) vs engine ({ex}, {ey}) -> "
            f"mismatch={mismatch} (delta {py_drop.x - ex:+.4f}, {py_drop.y - ey:+.4f})"
        )

        place_error = None
        try:
            furnace = ns.place_entity(Prototype.StoneFurnace, Direction.UP, py_drop)
            r.note(
                f"stone-furnace at reported drop_position SUCCEEDED at {furnace.position}"
            )
        except Exception as e:
            place_error = str(e)
            r.note(f"stone-furnace at reported drop_position FAILED: {place_error}")

        reproduced = mismatch or (place_error is not None)
        r.verdict(
            reproduced,
            "reported drop_position deviates from the engine value and/or cannot be "
            "used for the canonical furnace-under-the-drill placement"
            if reproduced
            else "drop_position matches engine and furnace placement at it works",
        )
        return 0 if reproduced else 1
    finally:
        inst.cleanup()


if __name__ == "__main__":
    sys.exit(main())
