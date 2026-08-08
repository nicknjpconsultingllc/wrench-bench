"""Integration/regression tests for FIX C (suspected production-stats swap).

Audit finding (see audit/README.md): the OLD get_production_stats/server.lua
contained TWO inversions -- input_counts (engine: produced) was stored in a
local named `consumption_diff`, output_counts (engine: consumed) in
`production_diff`, and the return table swapped them BACK
(output=consumption_diff, input=production_diff).  The external behavior was
therefore already correct: FLE "output" = produced, "input" = consumed,
matching every downstream consumer (profits.py, achievements.py,
observation_formatter.py).

FIX C removes the double inversion (straight mapping, correctly named
locals).  Because old and new behavior are identical, these tests pass on
both; their job is to LOCK the semantics end-to-end so that any future
single inversion -- the bug this audit went looking for -- fails loudly.
"""

from fle.env import Direction
from fle.env.entities import Position
from fle.env.game_types import Prototype


def _raw_counts(instance):
    out = instance.rcon_client.send_command(
        "/silent-command local st = game.forces.player.get_item_production_statistics(game.surfaces[1]) "
        "rcon.print(st.get_input_count('iron-plate')..','..st.get_output_count('iron-ore'))"
    )
    plate_produced, ore_consumed = (int(v) for v in out.split(","))
    return plate_produced, ore_consumed


def _delta(after, before, section, item):
    return after.get(section, {}).get(item, 0) - before.get(section, {}).get(item, 0)


def test_produced_goes_to_output_consumed_goes_to_input(instance):
    ns = instance.namespace
    raw_before = _raw_counts(instance)
    fle_before = ns._get_production_stats()

    furnace = ns.place_entity(Prototype.StoneFurnace, Direction.UP, Position(x=2, y=0))
    furnace = ns.insert_item(Prototype.Coal, furnace, 10)
    furnace = ns.insert_item(Prototype.IronOre, furnace, 10)
    ns.sleep(15)

    raw_after = _raw_counts(instance)
    fle_after = ns._get_production_stats()

    plates_produced = raw_after[0] - raw_before[0]
    ore_consumed = raw_after[1] - raw_before[1]
    assert plates_produced > 0, "smelting must have produced iron plates"
    assert ore_consumed > 0, "smelting must have consumed iron ore"

    # FLE convention: "output" = produced, "input" = consumed.
    assert _delta(fle_after, fle_before, "output", "iron-plate") == plates_produced
    assert _delta(fle_after, fle_before, "input", "iron-ore") == ore_consumed

    # And nothing leaked into the opposite buckets (a single-inversion
    # regression would put plates under "input" / ore under "output").
    assert _delta(fle_after, fle_before, "input", "iron-plate") == 0
    assert _delta(fle_after, fle_before, "output", "iron-ore") == 0


def test_achievements_see_smelted_plates_as_produced(instance):
    """Downstream consistency: calculate_achievements credits dynamic
    production from flows.output, so smelted plates must show up there."""
    from fle.commons.models.achievements import ProductionFlows
    from fle.env.utils.achievements import calculate_achievements

    ns = instance.namespace
    pre = ProductionFlows.from_dict(ns._get_production_stats())

    furnace = ns.place_entity(Prototype.StoneFurnace, Direction.UP, Position(x=2, y=0))
    furnace = ns.insert_item(Prototype.Coal, furnace, 10)
    furnace = ns.insert_item(Prototype.IronOre, furnace, 10)
    ns.sleep(15)

    post = ProductionFlows.from_dict(ns._get_production_stats())
    achievements = calculate_achievements(pre, post)

    assert achievements["dynamic"].get("iron-plate", 0) > 0, achievements
    assert "iron-ore" not in achievements["dynamic"], achievements
