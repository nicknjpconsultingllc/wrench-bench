"""Live regression tests for KINDS.entity_destruction's same_type_total
grouping fix (fle/env/tools/admin/inject_disruption/server.lua).

Background (see the module comment on REDUNDANCY_RADIUS in server.lua, and
fle.disruptions.scoring._redundancy_total's docstring): same_type_total used
to be a raw "how many entities share this name anywhere on the map" count,
which over-counted an unrelated, distant same-named entity (e.g. an
abandoned early build or an over-provisioned spare) that has no bearing on
the victim's actual production line -- inflating the passive-redundancy
floor floor_adjusted_throughput_retained_parts subtracts, and silently
mis-scoring genuine recovery as ~0 (or worse, the winsorize floor).

The fix groups candidates by the stronger of two signals before counting:

1. Same ``electric_network_id`` as the victim, when it has one (powered
   entities -- electric furnaces, drills, assembling machines).
2. A bounded radius (REDUNDANCY_RADIUS, 100 tiles) around the victim, for
   burner-tier entities (stone furnaces, burner mining drills) that have no
   electric network at all.

Neither is true functional/topological redundancy -- both are documented,
honestly-approximate mitigations, not a perfect fix (see server.lua's
comments). These tests prove each signal actually excludes an unrelated
entity live, and that a genuine redundant pair is still counted correctly
(no regression versus test_bracketing.py/test_floor_acceptance.py's
existing two-furnace scenarios).

This file connects directly to port 27003, NOT the shared tests/wrench
fixture (which owns 27000) or the other agents' dedicated ports (27001,
27002) -- other agents' live suites must not be disturbed. It defines its
own skip-if-absent instance fixture and is marked `wrench_live`, and
overrides the parent conftest's autouse reset fixture (which resolves
`instance` against 27000) with a no-op, in favor of this file's own
`_reset` fixture against 27003 -- mirrors test_floor_acceptance.py's
dedicated-port pattern exactly, including its port-27001 diagnostic note:
defensively clear a stale `game.tick_paused = true` on every reset, since
that can strand `move_to()` identically to a slow-chunk-gen stall but never
resolve.
"""

import socket

import pytest

pytestmark = pytest.mark.wrench_live

RCON_PORT = 27003
SEED = 1  # seeded_index(1, 3) == 1 (1-based): picks the first entity sorted by x


def seeded_index(seed: int, n: int) -> int:
    """Mirror of server.lua's LCG pick (1-based)."""
    x = (seed * 1103515245 + 12345) % 2147483648
    return (x % n) + 1


def _server_listening(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def redundancy_instance():
    """Direct connection to the live server on RCON :27003 (this file's
    dedicated port -- 27000/27001/27002 belong to other agents' live suites
    and must not be touched)."""
    if not _server_listening(RCON_PORT):
        pytest.skip(f"no live Factorio server on localhost:{RCON_PORT}")

    from fle.env import FactorioInstance

    inst = FactorioInstance(
        address="localhost",
        tcp_port=RCON_PORT,
        all_technologies_researched=True,
        cache_scripts=True,
        fast=True,
        inventory={
            "stone-furnace": 6,
            "coal": 500,
            "iron-ore": 500,
            "electric-furnace": 5,
            "small-electric-pole": 20,
        },
    )
    inst.rcon_client.send_command("/silent-command game.tick_paused = false")
    inst.set_speed(10.0)
    inst.default_initial_inventory = dict(inst.initial_inventory)
    try:
        yield inst
    finally:
        inst.rcon_client.send_command("/silent-command game.tick_paused = false")
        inst.rcon_client.send_command(
            "/silent-command for _, e in pairs(game.surfaces[1]"
            ".find_entities_filtered({name='electric-energy-interface'})) "
            "do e.destroy() end"
        )
        inst.reset(reset_position=True)
        inst.cleanup()


@pytest.fixture(autouse=True)
def _reset_between_tests():
    """Shadow the parent conftest's autouse fixture of the same name, which
    would otherwise resolve `instance` against port 27000 for any
    `wrench_live`-marked test."""
    yield


@pytest.fixture(autouse=True)
def _reset(redundancy_instance):
    redundancy_instance.rcon_client.send_command(
        "/silent-command game.tick_paused = false"
    )
    redundancy_instance.initial_inventory = dict(
        redundancy_instance.default_initial_inventory
    )
    redundancy_instance.reset(reset_position=True)
    redundancy_instance.set_speed(10.0)
    redundancy_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered({name='electric-energy-interface'})) "
        "do e.destroy() end"
    )
    redundancy_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered({type={'entity-ghost','tile-ghost'}})) "
        "do e.destroy() end"
    )
    redundancy_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1].find_entities_filtered({})) do "
        "if e.name:find('remnants') then e.destroy() end end"
    )
    engine = redundancy_instance.controllers["inject_disruption"]
    engine.reset_state()
    yield


def _fire_entity_destruction(instance, seed=SEED, timeout_game_seconds=120):
    game = instance.namespace
    engine = instance.controllers["inject_disruption"]
    disruption_id = engine.arm(
        kind="entity_destruction",
        seed=seed,
        quota_item="iron-plate",
        quota_per_min=1,
        quota_fraction=0.0,
        consecutive_windows=1,
        window_ticks=60,
    )
    engine.fire_now(disruption_id)

    waited = 0
    while waited < timeout_game_seconds:
        game.sleep(2)
        waited += 2
        for ev in engine.drain_events():
            if ev["event"] == "fired":
                return ev
    raise AssertionError(
        f"disruption did not fire within {timeout_game_seconds} game-seconds"
    )


def add_power_source(instance, position):
    """Admin-only ``electric-energy-interface`` power source next to
    ``position`` (mirrors test_floor_acceptance.py's helper of the same
    name)."""
    x, y = position
    instance.rcon_client.send_command(
        "/silent-command local iface = game.surfaces[1].create_entity{"
        f"name='electric-energy-interface', position={{{x}, {y}}}, "
        "force='player'} "
        "iface.power_production = 5000000 "
        "iface.electric_buffer_size = 5000000 "
        "iface.energy = 5000000"
    )


class TestBoundedRadiusGrouping:
    """Burner-tier fallback (no electric_network_id): only same-named
    candidates within REDUNDANCY_RADIUS (100 tiles) of the victim count."""

    def test_distant_unrelated_furnace_is_excluded(self, redundancy_instance):
        from fle.env.entities import Position
        from fle.env.game_types import Prototype

        game = redundancy_instance.namespace
        game.move_to(Position(x=0, y=0))
        game.sleep(3)

        # Two genuinely redundant stone furnaces, close together -- same
        # pattern as test_bracketing.py's build_two_furnace_factory.
        near_positions = [(2.0, 0.0), (6.0, 0.0)]
        for x, y in near_positions:
            furnace = game.place_entity(
                Prototype.StoneFurnace, position=Position(x=x, y=y)
            )
            game.insert_item(Prototype.Coal, furnace, quantity=50)
            game.insert_item(Prototype.IronOre, furnace, quantity=50)

        # A third, distant, unrelated stone furnace (304 tiles from the
        # victim -- well outside REDUNDANCY_RADIUS). Created via raw admin
        # RCON rather than game.place_entity/move_to to avoid a long-
        # distance move_to and the documented tick_paused/chunk-gen stall
        # risk (test_floor_acceptance.py's module docstring).
        redundancy_instance.rcon_client.send_command(
            "/silent-command game.surfaces[1].create_entity{"
            "name='stone-furnace', position={306.0, 0.0}, force='player'}"
        )

        # Sanity: the seeded pick lands on the expected near furnace (sorted
        # by x among all 3 candidates), confirming the fixture and seed
        # agree with seeded_index's mirror.
        ordered = sorted(near_positions + [(306.0, 0.0)])
        expected_victim = ordered[seeded_index(SEED, len(ordered)) - 1]

        fired = _fire_entity_destruction(redundancy_instance)
        assert fired["kind"] == "entity_destruction"
        victim = fired["affected"][0]
        assert victim["name"] == "stone-furnace"
        assert (victim["x"], victim["y"]) == expected_victim

        # The genuine pair is counted (2); the distant unrelated furnace is
        # excluded -- pre-fix this was 3 (raw same-name count over the
        # whole candidate pool, no distance/network check).
        assert victim["same_type_total"] == 2, (
            f"expected same_type_total=2 (genuine pair only, distant "
            f"unrelated furnace excluded), got {victim['same_type_total']}"
        )


class TestElectricNetworkGrouping:
    """Preferred signal (electric_network_id present): only same-named
    candidates sharing the victim's exact network count, regardless of
    whether an unrelated same-named entity on a DIFFERENT network happens to
    be within REDUNDANCY_RADIUS."""

    def test_different_network_furnace_within_radius_is_excluded(
        self, redundancy_instance
    ):
        from fle.env.entities import Position, Direction
        from fle.env.game_types import Prototype

        game = redundancy_instance.namespace
        game.move_to(Position(x=0, y=0))
        game.sleep(3)

        # Network A: two electric furnaces, each with its own nearby pole
        # (a single pole's supply area is too small to cover both at once)
        # -- the two poles auto-wire together (within the 7.5-tile wire
        # reach) into one shared electric network.
        network_a_positions = [(2.0, 0.0), (6.0, 0.0)]
        for x, y in network_a_positions:
            furnace = game.place_entity(
                Prototype.ElectricFurnace, position=Position(x=x, y=y)
            )
            game.insert_item(Prototype.IronOre, furnace, quantity=50)
            game.place_entity_next_to(
                Prototype.SmallElectricPole,
                reference_position=furnace.position,
                direction=Direction.UP,
                spacing=0,
            )
        add_power_source(
            redundancy_instance,
            (network_a_positions[0][0], network_a_positions[0][1] - 3),
        )

        # Network B: a separate, unwired electric furnace + pole, 20 tiles
        # away from network A -- well within REDUNDANCY_RADIUS (100) but on
        # a different electric network (its pole is placed on the opposite
        # side from network A's poles, far outside the 7.5-tile wire reach,
        # so the two networks never auto-merge).
        bx, by = (20.0, 0.0)
        game.move_to(Position(x=bx, y=by))
        furnace_b = game.place_entity(
            Prototype.ElectricFurnace, position=Position(x=bx, y=by)
        )
        game.insert_item(Prototype.IronOre, furnace_b, quantity=50)
        pole_b = game.place_entity_next_to(
            Prototype.SmallElectricPole,
            reference_position=furnace_b.position,
            direction=Direction.DOWN,
            spacing=0,
        )
        add_power_source(
            redundancy_instance, (pole_b.position.x, pole_b.position.y + 1)
        )

        fired = _fire_entity_destruction(redundancy_instance)
        assert fired["kind"] == "entity_destruction"
        victim = fired["affected"][0]
        assert victim["name"] == "electric-furnace"
        # Not asserting an exact victim position here (unlike the stone-
        # furnace radius test below): electric-furnace is an odd-sized (3x3)
        # entity, so its actual placed center is offset by (0.5, 0.5) from
        # the requested corner position -- brittle to hardcode, and not the
        # point of this test (same_type_total is).
        assert victim["x"] != bx or victim["y"] != by, (
            "seeded pick landed on the different-network furnace -- adjust "
            "SEED so the victim is one of network A's furnaces"
        )

        # The genuine same-network pair is counted (2); the different-
        # network furnace is excluded even though it sits well within
        # REDUNDANCY_RADIUS -- proves the network signal is actually used
        # (and preferred over the radius fallback) for powered entities.
        assert victim["same_type_total"] == 2, (
            f"expected same_type_total=2 (same-network pair only, "
            f"different-network furnace excluded despite being in radius), "
            f"got {victim['same_type_total']}"
        )
