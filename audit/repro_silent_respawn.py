"""Repro for NEW suspected bug: silent character respawn with inventory wipe.

fle/env/mods/utils.lua storage.utils.ensure_valid_character(player_index):
every tool call routes through this; if the agent's character has died it
silently creates a brand-new character at the spawn origin with an EMPTY
inventory.  Nothing in the next observation tells the agent that it died,
teleported, or lost everything it was carrying.

Repro:
  1. Give the agent items; move it away from origin; record inventory.
  2. Kill the character via raw Lua (.die()).
  3. Run an ordinary inst.eval() tool call and inspect the observation:
     expected: some indication of death / respawn / inventory loss;
     actual: a perfectly normal observation, with the inventory now empty
     and the character back at origin.

PASS = inventory wiped + position reset + no death/respawn indication in
the observation returned to the agent.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, lua, Reporter

DEATH_WORDS = ("died", "death", "respawn", "killed", "dead", "lost", "wiped")


def main():
    r = Reporter("NEW-5", "silent character respawn with inventory wipe")

    inst = connect(inventory={"iron-plate": 5, "coal": 3})
    try:
        inst.reset()
        ns = inst.namespace

        from fle.env.entities import Position

        ns.move_to(Position(x=10, y=10))
        inv_before = ns.inspect_inventory()
        pos_before = lua(
            inst,
            "local c = storage.agent_characters[1] rcon.print(c.position.x..','..c.position.y)",
        )
        r.note(f"inventory before death: {inv_before}")
        r.note(f"character position before death: ({pos_before})")

        out = lua(
            inst,
            "local c = storage.agent_characters[1] if c and c.valid then c.die() "
            "rcon.print('character killed') else rcon.print('no character') end",
        )
        r.note(f"killed character via Lua: {out}")

        # An ordinary tool call, exactly as an agent would issue it.
        score, _, observation = inst.eval("print(inspect_inventory())")
        r.note(f"next observation after death: {observation!r}")

        inv_after = ns.inspect_inventory()
        pos_after = lua(
            inst,
            "local c = storage.agent_characters[1] rcon.print(c.position.x..','..c.position.y)",
        )
        r.note(f"inventory after: {inv_after}; character position after: ({pos_after})")

        obs_lower = observation.lower()
        mentions_death = any(w in obs_lower for w in DEATH_WORDS)
        inventory_wiped = len(dict(inv_after)) == 0 and len(dict(inv_before)) > 0
        teleported = pos_after != pos_before

        r.note(
            f"observation mentions death/respawn: {mentions_death}; "
            f"inventory silently wiped: {inventory_wiped}; "
            f"character silently teleported to origin: {teleported}"
        )
        reproduced = inventory_wiped and teleported and not mentions_death
        r.verdict(
            reproduced,
            "the agent's character died, respawned at origin with an empty inventory, "
            "and the next observation contained no indication of any of it"
            if reproduced
            else "death/respawn is now surfaced to the agent (or inventory survived)",
        )
        return 0 if reproduced else 1
    finally:
        # Clean up the corpse so later runs/tests aren't blocked by it.
        try:
            lua(
                inst,
                'for _, e in pairs(game.surfaces[1].find_entities_filtered{type="character-corpse"}) do e.destroy() end',
            )
        except Exception:
            pass
        inst.cleanup()


if __name__ == "__main__":
    sys.exit(main())
