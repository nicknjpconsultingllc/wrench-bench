"""Repro for NEW suspected bug: production statistics input/output label swap.

fle/env/tools/admin/get_production_stats/server.lua reads Factorio's
LuaFlowStatistics where (per the Factorio API, and verified empirically
below with raw RCON):
    input_counts  = items PRODUCED   (left column of the production GUI)
    output_counts = items CONSUMED   (right column)

The suspicion: the Lua assigns input_counts into a local called
`consumption_diff` and output_counts into `production_diff` -- an inversion.

This script measures what actually crosses the API boundary end-to-end:
  1. Snapshot raw engine counters and FLE's _get_production_stats().
  2. Smelt iron plates in a stone furnace (insert coal + iron-ore, sleep).
  3. Snapshot again; compare deltas:
       raw   get_input_count("iron-plate")  must rise  (= production)
       raw   get_output_count("iron-ore")   must rise  (= consumption)
       FLE   flows["output"]["iron-plate"]  SHOULD rise (FLE convention:
             output = produced, per profits.py / achievements.py /
             observation_formatter.py which all read "output" as produced)
       FLE   flows["input"]["iron-ore"]     SHOULD rise (input = consumed)

PASS = the swap is visible at the Python API boundary, i.e. FLE reports the
produced item (iron-plate) under "input" and/or the consumed item
(iron-ore) under "output".
FAIL = labels are correct end-to-end at the boundary (see README: the Lua
contains TWO inversions -- misnamed locals AND a swapped return table --
which cancel out).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, lua, Reporter


def raw_counts(inst):
    out = lua(
        inst,
        "local st = game.forces.player.get_item_production_statistics(game.surfaces[1]) "
        "rcon.print(st.get_input_count('iron-plate')..','..st.get_output_count('iron-plate')..','"
        "..st.get_input_count('iron-ore')..','..st.get_output_count('iron-ore'))",
    )
    a, b, c, d = (int(v) for v in out.split(","))
    return {
        "plate_input(produced)": a,
        "plate_output(consumed)": b,
        "ore_input(produced)": c,
        "ore_output(consumed)": d,
    }


def main():
    r = Reporter("NEW-4", "production stats input/output label swap")

    inst = connect(inventory={"stone-furnace": 1, "iron-ore": 20, "coal": 20})
    try:
        inst.reset()
        inst.set_speed(10)
        ns = inst.namespace

        from fle.env.game_types import Prototype
        from fle.env.entities import Position
        from fle.env import Direction

        raw_before = raw_counts(inst)
        fle_before = ns._get_production_stats()
        r.note(f"raw engine counters before: {raw_before}")

        furnace = ns.place_entity(
            Prototype.StoneFurnace, Direction.UP, Position(x=2, y=0)
        )
        furnace = ns.insert_item(Prototype.Coal, furnace, 10)
        furnace = ns.insert_item(Prototype.IronOre, furnace, 10)
        ns.sleep(15)

        raw_after = raw_counts(inst)
        fle_after = ns._get_production_stats()
        r.note(f"raw engine counters after smelting: {raw_after}")

        d_plate_produced = (
            raw_after["plate_input(produced)"] - raw_before["plate_input(produced)"]
        )
        d_ore_consumed = (
            raw_after["ore_output(consumed)"] - raw_before["ore_output(consumed)"]
        )
        r.note(
            f"raw deltas: input_counts['iron-plate'] +{d_plate_produced} (plates PRODUCED), "
            f"output_counts['iron-ore'] +{d_ore_consumed} (ore CONSUMED)"
        )
        if d_plate_produced <= 0:
            r.note("smelting did not register; repro inconclusive")
            r.verdict(False, "no plates produced -- rerun")
            return 1

        def delta(flows_after, flows_before, section, item):
            return flows_after.get(section, {}).get(item, 0) - flows_before.get(
                section, {}
            ).get(item, 0)

        fle_out_plate = delta(fle_after, fle_before, "output", "iron-plate")
        fle_in_plate = delta(fle_after, fle_before, "input", "iron-plate")
        fle_out_ore = delta(fle_after, fle_before, "output", "iron-ore")
        fle_in_ore = delta(fle_after, fle_before, "input", "iron-ore")
        r.note(
            f"FLE _get_production_stats deltas: output[iron-plate]=+{fle_out_plate}, "
            f"input[iron-plate]=+{fle_in_plate}, output[iron-ore]=+{fle_out_ore}, "
            f"input[iron-ore]=+{fle_in_ore}"
        )

        # FLE convention (profits.py, achievements.py, observation_formatter.py):
        # "output" = produced, "input" = consumed.
        swap_visible = (fle_in_plate > 0 and fle_out_plate == 0) or (
            fle_out_ore > 0 and fle_in_ore == 0
        )
        correct = fle_out_plate == d_plate_produced and fle_in_ore == d_ore_consumed
        r.note(
            f"swap visible at API boundary: {swap_visible}; "
            f"labels correct end-to-end: {correct}"
        )
        r.verdict(
            swap_visible,
            "FLE reports produced items as consumed (and vice versa)"
            if swap_visible
            else "labels are correct at the API boundary: the two internal inversions in "
            "server.lua (misnamed locals + swapped return keys) cancel out",
        )
        return 0 if swap_visible else 1
    finally:
        inst.cleanup()


if __name__ == "__main__":
    sys.exit(main())
