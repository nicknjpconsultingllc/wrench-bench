"""Throughput-Retained bracketing tests against the live WRENCH server.

Scripted fixture agents (no LLM):

- build the standard two-hand-fed-furnace iron-plate setup (~37 plates/min),
- arm a seeded entity_destruction gated on a demonstrated-throughput
  precondition, and let the engine fire it,
- then either do nothing (no-op agent) or immediately place and stock a
  replacement furnace (oracle agent).

TR over a 3600-tick post-fire horizon must bracket: the no-op agent lands
near 0.5 (one of two identical furnaces died; the killed furnace stops
instantly and its spilled contents never smelt, while the survivor keeps
its full rate -- so retained throughput is the surviving half), and the
oracle must beat the no-op by a clear margin.

Marked wrench_live: skipped automatically when no server listens on :27000.
"""

import pytest

from fle.disruptions.scoring import throughput_retained
from fle.env.entities import Position
from fle.env.game_types import Prototype

pytestmark = pytest.mark.wrench_live

QUOTA_PER_MIN = 30
SEED = 11
FURNACE_POSITIONS = [(2.0, 0.0), (6.0, 0.0)]
TR_HORIZON_TICKS = 3600


def seeded_index(seed: int, n: int) -> int:
    """Mirror of server.lua's LCG pick (1-based)."""
    x = (seed * 1103515245 + 12345) % 2147483648
    return (x % n) + 1


def build_two_furnace_factory(game):
    """Standard kill-test setup: two hand-fed stone furnaces (~37 plates/min)."""
    game.move_to(Position(x=0, y=0))
    furnaces = []
    for x, y in FURNACE_POSITIONS:
        furnace = game.place_entity(Prototype.StoneFurnace, position=Position(x=x, y=y))
        furnace = game.insert_item(Prototype.Coal, furnace, quantity=150)
        furnace = game.insert_item(Prototype.IronOre, furnace, quantity=190)
        furnaces.append(furnace)
    return furnaces


def arm_destruction(engine, seed=SEED, delay_ticks=0):
    return engine.arm(
        kind="entity_destruction",
        seed=seed,
        quota_item="iron-plate",
        quota_per_min=QUOTA_PER_MIN,
        quota_fraction=0.5,
        consecutive_windows=2,
        window_ticks=1800,
        delay_ticks=delay_ticks,
    )


def wait_for_fired(engine, game, timeout_game_seconds=240):
    """Sleep in short game-time slices until the engine reports a fire."""
    events = []
    waited = 0
    while waited < timeout_game_seconds:
        game.sleep(5)
        waited += 5
        events.extend(engine.drain_events())
        fired = [e for e in events if e["event"] == "fired"]
        if fired:
            return fired[0], events
    raise AssertionError(
        f"disruption did not fire within {timeout_game_seconds} game-seconds; "
        f"events so far: {events}"
    )


def sleep_ticks(game, instance, until_tick, margin_ticks=200):
    """Sleep game-time until game.tick passes until_tick (+margin)."""
    while True:
        current = int(
            instance.rcon_client.send_command("/silent-command rcon.print(game.tick)")
        )
        if current >= until_tick + margin_ticks:
            return current
        remaining_s = (until_tick + margin_ticks - current) / 60
        game.sleep(min(15, max(1, int(remaining_s) + 1)))


def expected_victim():
    """The furnace the seeded LCG must pick among the two, sorted by x."""
    ordered = sorted(FURNACE_POSITIONS)
    return ordered[seeded_index(SEED, len(ordered)) - 1]


def run_disruption_scenario(instance, oracle: bool):
    """Build, arm, wait for the fire, optionally repair; return TR."""
    instance.set_speed(10.0)
    game = instance.namespace
    engine = instance.controllers["inject_disruption"]
    engine.reset_state()

    build_two_furnace_factory(game)
    arm_destruction(engine)

    fired, _ = wait_for_fired(engine, game)
    fire_tick = fired["tick"]

    # Exactly one victim, and it is the seed-determined furnace.
    assert fired["kind"] == "entity_destruction"
    assert len(fired["affected"]) == 1
    victim = fired["affected"][0]
    assert victim["name"] == "stone-furnace"
    assert (victim["x"], victim["y"]) == expected_victim()

    # The world agrees: one furnace remains.
    remaining = game.get_entities({Prototype.StoneFurnace})
    assert len(remaining) == 1

    if oracle:
        # Immediate repair: replacement furnace at a free spot, restocked.
        game.move_to(Position(x=0, y=0))
        replacement = game.place_entity(
            Prototype.StoneFurnace, position=Position(x=4, y=3)
        )
        replacement = game.insert_item(Prototype.Coal, replacement, quantity=150)
        game.insert_item(Prototype.IronOre, replacement, quantity=190)

    sleep_ticks(game, instance, fire_tick + TR_HORIZON_TICKS)

    samples = engine.samples()
    tr = throughput_retained(samples, "iron-plate", fire_tick, TR_HORIZON_TICKS)
    assert tr is not None, "baseline should be well-defined by the precondition"
    return tr


def test_tr_bracketing_noop_vs_oracle(instance):
    """No-op TR ~0.5; oracle TR materially higher."""
    noop_tr = run_disruption_scenario(instance, oracle=False)
    # One of two identical furnaces died: the surviving half keeps producing,
    # the victim's buffered ore is spilled (never smelted), so retained
    # throughput sits near 0.5 of the frozen two-furnace baseline.
    assert 0.25 <= noop_tr <= 0.75, f"no-op TR {noop_tr} outside expected band"

    instance.reset(reset_position=True)

    oracle_tr = run_disruption_scenario(instance, oracle=True)
    # Relative bracketing, not absolutes: the oracle loses only the repair
    # latency (a few hundred ticks) out of the 3600-tick horizon.
    assert oracle_tr > noop_tr + 0.2, (
        f"oracle TR {oracle_tr} not materially above no-op TR {noop_tr}"
    )
    print(f"noop_tr={noop_tr:.3f} oracle_tr={oracle_tr:.3f}")


def test_victim_selection_is_deterministic(instance):
    """Same seed + same layout -> same victim, across a full reset."""
    victims = []
    for attempt in range(2):
        if attempt:
            instance.reset(reset_position=True)
        game = instance.namespace
        engine = instance.controllers["inject_disruption"]
        engine.reset_state()

        build_two_furnace_factory(game)
        disruption_id = arm_destruction(engine)
        engine.fire_now(disruption_id)

        fired, _ = wait_for_fired(engine, game, timeout_game_seconds=30)
        assert len(fired["affected"]) == 1
        victim = fired["affected"][0]
        victims.append((victim["name"], victim["x"], victim["y"]))

    assert victims[0] == victims[1]
    assert victims[0] == ("stone-furnace", *expected_victim())
