"""adaptive_strike: proves the engine targets the live build's topology.

Unlike entity_destruction/belt_cut/resource_exhaustion (deterministic seeded
pick over a filtered entity list), adaptive_strike analyzes the actual
electric-pole network at fire time and strikes whichever pole feeds the most
powered consumers -- the simplest legible "single point of failure" proxy
(see server.lua KINDS.adaptive_strike for the full heuristic writeup).

This file connects directly to port 27002, NOT the shared tests/wrench
fixture (which owns 27000) -- other agents' live suites must not be
disturbed. It defines its own skip-if-absent instance fixture and does not
use the `wrench_live` marker, so the parent conftest's autouse reset fixture
(which resolves `instance` against 27000) never engages here.
"""

import socket

import pytest

from fle.disruptions.scoring import throughput_retained
from fle.env.entities import Position
from fle.env.game_types import Prototype

ADAPTIVE_RCON_PORT = 27002
SEED = 29
QUOTA_PER_MIN = 20
TR_HORIZON_TICKS = 3600


def _server_listening(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def adaptive_instance():
    """Direct connection to the live server on RCON :27002 (this task's
    dedicated port -- 27000/27001 belong to other agents and must not be
    touched)."""
    if not _server_listening(ADAPTIVE_RCON_PORT):
        pytest.skip(f"no live Factorio server on localhost:{ADAPTIVE_RCON_PORT}")

    from fle.env import FactorioInstance

    inst = FactorioInstance(
        address="localhost",
        tcp_port=ADAPTIVE_RCON_PORT,
        all_technologies_researched=True,
        cache_scripts=True,
        fast=True,
        inventory={
            "electric-furnace": 10,
            "small-electric-pole": 20,
            "iron-ore": 2000,
        },
    )
    inst.set_speed(10.0)
    inst.default_initial_inventory = dict(inst.initial_inventory)
    try:
        yield inst
    finally:
        inst.cleanup()


@pytest.fixture(autouse=True)
def _reset(adaptive_instance):
    adaptive_instance.initial_inventory = dict(adaptive_instance.default_initial_inventory)
    adaptive_instance.reset(reset_position=True)
    adaptive_instance.set_speed(10.0)
    # reset()'s clear_entities only clears what the normal game systems
    # track; the admin-only electric-energy-interface power source (created
    # via raw RCON, not place_entity) survives it and would otherwise block
    # the next test's pole placement at the same spot.
    adaptive_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered({name='electric-energy-interface'})) "
        "do e.destroy() end"
    )
    engine = adaptive_instance.controllers["inject_disruption"]
    engine.reset_state()
    yield


def wait_for_fired(engine, game, timeout_game_seconds=120):
    events = []
    waited = 0
    while waited < timeout_game_seconds:
        game.sleep(2)
        waited += 2
        events.extend(engine.drain_events())
        fired = [e for e in events if e["event"] == "fired"]
        if fired:
            return fired[0], events
    raise AssertionError(
        f"adaptive_strike did not fire within {timeout_game_seconds} "
        f"game-seconds; events so far: {events}"
    )


def sleep_ticks(game, instance, until_tick, margin_ticks=200):
    while True:
        current = int(
            instance.rcon_client.send_command("/silent-command rcon.print(game.tick)")
        )
        if current >= until_tick + margin_ticks:
            return current
        remaining_s = (until_tick + margin_ticks - current) / 60
        game.sleep(min(15, max(1, int(remaining_s) + 1)))


def add_power_source(instance, position):
    """Wire an admin-only ``electric-energy-interface`` next to ``position``
    as a constant, day/night-independent power source for the floor-
    acceptance test (which needs real production, not just topology).
    Not a player-buildable entity -- created directly via RCON, the same
    bulk-access pattern the engine itself uses for events/samples."""
    x, y = position
    instance.rcon_client.send_command(
        "/silent-command local iface = game.surfaces[1].create_entity{"
        f"name='electric-energy-interface', position={{{x}, {y}}}, "
        "force='player'} "
        "iface.power_production = 5000000 "
        "iface.electric_buffer_size = 5000000 "
        "iface.energy = 5000000"
    )


def arm_adaptive(engine, seed=SEED):
    return engine.arm(
        kind="adaptive_strike",
        seed=seed,
        quota_item="iron-plate",
        quota_per_min=QUOTA_PER_MIN,
        quota_fraction=0.5,
        consecutive_windows=1,
        window_ticks=600,
    )


def build_fragile_factory(game, instance=None, powered=False):
    """Single electric furnace on its own single power pole: the whole
    build's power depends on one entity.

    ``powered``: also add a constant admin power source next to the pole so
    the furnace actually produces (needed for the floor-acceptance TR
    measurement; the pure targeting tests don't need real production, just
    the entities to exist).
    """
    game.move_to(Position(x=0, y=0))
    pole = game.place_entity(
        Prototype.SmallElectricPole, position=Position(x=0, y=-2)
    )
    furnace = game.place_entity(
        Prototype.ElectricFurnace, position=Position(x=0, y=0)
    )
    game.insert_item(Prototype.IronOre, furnace, quantity=190)
    if powered:
        add_power_source(instance, (pole.position.x, pole.position.y - 1))
    return {"pole": (pole.position.x, pole.position.y)}


def build_redundant_factory(game):
    """Two independent power lines, far enough apart that they do not
    auto-wire into a shared network:

    - line A: one pole feeding TWO electric furnaces (the busier, more
      load-bearing pole -- this is the one adaptive_strike should pick)
    - line B: one pole feeding a single electric furnace

    Both lines are individually "redundant" in the sense the benchmark cares
    about (killing either line's furnace only costs that line's output), but
    they are not symmetric: line A's pole is genuinely more load-bearing,
    and adaptive_strike must find that by analysis, not by array order (line
    B's furnace is placed and would sort first).
    """
    # Line B first (placed first -> would win a naive "first entity" pick).
    # Placed north of spawn (confirmed water-free strip).
    game.move_to(Position(x=0, y=-10))
    pole_b = game.place_entity(
        Prototype.SmallElectricPole, position=Position(x=0, y=-10)
    )
    furnace_b = game.place_entity(
        Prototype.ElectricFurnace, position=Position(x=0, y=-7)
    )
    game.insert_item(Prototype.IronOre, furnace_b, quantity=190)

    # Line A: one pole, two furnaces -- the higher-load target. Placed east
    # of spawn (confirmed water-free strip), well outside small-pole wire
    # reach from line B so the two networks stay separate.
    game.move_to(Position(x=10, y=0))
    pole_a = game.place_entity(
        Prototype.SmallElectricPole, position=Position(x=10, y=-2)
    )
    furnace_a1 = game.place_entity(
        Prototype.ElectricFurnace, position=Position(x=8, y=0)
    )
    game.insert_item(Prototype.IronOre, furnace_a1, quantity=190)
    furnace_a2 = game.place_entity(
        Prototype.ElectricFurnace, position=Position(x=12, y=0)
    )
    game.insert_item(Prototype.IronOre, furnace_a2, quantity=190)

    return {
        "pole_a": (pole_a.position.x, pole_a.position.y),
        "pole_b": (pole_b.position.x, pole_b.position.y),
    }


class TestAdaptivity:
    def test_fragile_build_loses_its_only_pole(self, adaptive_instance):
        game = adaptive_instance.namespace
        engine = adaptive_instance.controllers["inject_disruption"]

        layout = build_fragile_factory(game)
        disruption_id = arm_adaptive(engine)
        engine.fire_now(disruption_id)

        fired, _ = wait_for_fired(engine, game)
        assert fired["kind"] == "adaptive_strike"
        assert len(fired["affected"]) == 1
        victim = fired["affected"][0]
        assert victim["name"] == "small-electric-pole"
        assert (victim["x"], victim["y"]) == layout["pole"]
        # its only consumer was disconnected
        assert victim["consumers_disconnected"] == 1

        remaining_poles = game.get_entities({Prototype.SmallElectricPole})
        assert len(remaining_poles) == 0

    def test_redundant_build_picks_the_busier_pole(self, adaptive_instance):
        game = adaptive_instance.namespace
        engine = adaptive_instance.controllers["inject_disruption"]

        layout = build_redundant_factory(game)
        disruption_id = arm_adaptive(engine)
        engine.fire_now(disruption_id)

        fired, _ = wait_for_fired(engine, game)
        assert fired["kind"] == "adaptive_strike"
        assert len(fired["affected"]) == 1
        victim = fired["affected"][0]
        assert victim["name"] == "small-electric-pole"
        # Must pick line A's pole (feeds 2 furnaces), NOT line B's (placed
        # first, would win a naive array-order pick) or either furnace.
        assert (victim["x"], victim["y"]) == layout["pole_a"]
        assert victim["consumers_disconnected"] == 2

        remaining_poles = game.get_entities({Prototype.SmallElectricPole})
        assert len(remaining_poles) == 1
        assert (
            round(remaining_poles[0].position.x),
            round(remaining_poles[0].position.y),
        ) == (round(layout["pole_b"][0]), round(layout["pole_b"][1]))

    def test_targets_differ_between_fragile_and_redundant_topology(
        self, adaptive_instance
    ):
        """The core adaptivity claim in one test: same spec (same seed),
        different builds -> different, topology-appropriate victims."""
        game = adaptive_instance.namespace
        engine = adaptive_instance.controllers["inject_disruption"]

        build_fragile_factory(game)
        d1 = arm_adaptive(engine)
        engine.fire_now(d1)
        fired1, _ = wait_for_fired(engine, game)
        victim1 = (fired1["affected"][0]["x"], fired1["affected"][0]["y"])
        damage1 = fired1["affected"][0]["consumers_disconnected"]

        adaptive_instance.reset(reset_position=True)
        engine.reset_state()

        build_redundant_factory(game)
        d2 = arm_adaptive(engine)
        engine.fire_now(d2)
        fired2, _ = wait_for_fired(engine, game)
        victim2 = (fired2["affected"][0]["x"], fired2["affected"][0]["y"])
        damage2 = fired2["affected"][0]["consumers_disconnected"]

        assert victim1 != victim2, (
            "adaptive_strike picked the same coordinates on two different "
            "topologies -- targeting is not actually reading the build"
        )
        # Fragile build: 1 consumer lost. Redundant build: the busier
        # pole (2 consumers) is still the right call, even though the
        # build overall has more redundancy than the fragile one.
        assert damage1 == 1
        assert damage2 == 2


class TestFloorAcceptance:
    def test_noop_throughput_falls_to_floor(self, adaptive_instance):
        """Same bar as the other v1 kinds (README): a no-op agent's
        post-injection throughput must fall to <=0.2x the frozen baseline.
        Uses the fragile build, where adaptive_strike's SPOF pick kills
        100% of the power on the build by construction."""
        game = adaptive_instance.namespace
        engine = adaptive_instance.controllers["inject_disruption"]
        engine.track("iron-plate")

        build_fragile_factory(game, instance=adaptive_instance, powered=True)

        # Let it run and prove throughput before arming.
        for _ in range(6):
            game.sleep(5)

        disruption_id = arm_adaptive(engine)
        engine.fire_now(disruption_id)
        fired, _ = wait_for_fired(engine, game)
        fire_tick = fired["tick"]

        sleep_ticks(game, adaptive_instance, fire_tick + TR_HORIZON_TICKS)

        samples = engine.samples()
        tr = throughput_retained(samples, "iron-plate", fire_tick, TR_HORIZON_TICKS)
        assert tr is not None, "baseline should be well-defined"
        assert tr <= 0.2, f"no-op TR {tr} above the 0.2x floor-acceptance bar"
        print(f"floor-acceptance no-op TR={tr:.3f}")
