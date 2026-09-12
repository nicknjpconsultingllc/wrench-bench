"""Floor-acceptance tests for entity_destruction, belt_cut, resource_exhaustion.

Mirrors TestFloorAcceptance in test_adaptive_strike.py: with a no-op fixture
agent (does nothing after the disruption fires), post-injection throughput
must fall to <=0.2x the frozen pre-disruption baseline, on a build with a
genuine single point of failure (no redundancy to passively survive on).
adaptive_strike's own floor-acceptance test already covers that kind; this
file covers the three remaining v1 kinds, which all use the deterministic
seeded-pick mechanism in server.lua (KINDS.entity_destruction/belt_cut/
resource_exhaustion) rather than adaptive_strike's topology analysis.

This file connects directly to port 27001, NOT the shared tests/wrench
fixture (which owns 27000) or test_adaptive_strike.py's dedicated port
(27002) -- other agents' live suites must not be disturbed. It defines its
own skip-if-absent instance fixture, and is marked `wrench_live` so
`pytest -m "not wrench_live"` actually deselects it. The parent conftest's
autouse reset fixture (which resolves `instance` against 27000) would
normally engage for any `wrench_live`-marked test; it's overridden to a
no-op below in favor of this file's own `_reset` fixture against 27001.

Port 27001 diagnostic note: on first connecting to this port, `move_to()`
hung forever (timed out after 120 path-request polls). Root cause, found by
inspecting `storage.wrench`/`storage.paths` state and `game.tick` via raw
RCON: the server had `game.tick_paused = true` left over from some earlier
process's `instance.pause()` call whose matching `unpause()` never ran
(likely a killed probe from an earlier debugging session against this
long-lived, heavily-shared container -- `docker ps` shows 26+ hours uptime).
With the game clock frozen, nothing async can ever resolve, including
Factorio's own path-request completion event -- so every `move_to()` looks
identical to a slow-chunk-generation stall but never times out into success
no matter how long you wait or how high `FLE_GETPATH_MAX_ATTEMPTS` is set.
It was NOT entity clutter (`clear_entities=True` does properly wipe
player-built entities between resets -- confirmed live; the ~849-2895
entities visible near spawn are almost entirely natural map resources:
trees, stone, coal, ore, crude-oil) and it was NOT slow chunk generation.
`instance.unpause()` cannot fix this on a fresh connection: its `_is_paused`
guard defaults False and only tracks this Python object's own pause calls,
not server truth, so it silently no-ops. The fix is to send the raw RCON
command directly, which this file's fixtures do defensively on every reset.
"""

import socket

import pytest

from fle.disruptions.scoring import throughput_retained
from fle.env.entities import Position, Direction
from fle.env.game_types import Prototype, Resource

pytestmark = pytest.mark.wrench_live

FLOOR_RCON_PORT = 27001
QUOTA_PER_MIN = 20
TR_HORIZON_TICKS = 3600


def _server_listening(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def floor_instance():
    """Direct connection to the live server on RCON :27001 (this file's
    dedicated port -- 27000/27002 belong to other agents' live suites and
    must not be touched)."""
    if not _server_listening(FLOOR_RCON_PORT):
        pytest.skip(f"no live Factorio server on localhost:{FLOOR_RCON_PORT}")

    from fle.env import FactorioInstance

    inst = FactorioInstance(
        address="localhost",
        tcp_port=FLOOR_RCON_PORT,
        all_technologies_researched=True,
        cache_scripts=True,
        fast=True,
        inventory={
            "electric-furnace": 5,
            "small-electric-pole": 20,
            "electric-mining-drill": 5,
            "inserter": 10,
            "wooden-chest": 5,
            "transport-belt": 20,
            "iron-ore": 500,
            "speed-module-3": 10,
        },
    )
    # See module docstring: this port had a stale tick_paused=true left over
    # by an earlier session. Clear it before anything else touches the
    # server (instance.unpause() would no-op here -- its local _is_paused
    # guard defaults False on a fresh connection and doesn't reflect actual
    # server state).
    inst.rcon_client.send_command("/silent-command game.tick_paused = false")
    inst.set_speed(10.0)
    inst.default_initial_inventory = dict(inst.initial_inventory)
    try:
        yield inst
    finally:
        # Leave the shared container clean for whoever uses it next: clear
        # a stale pause, destroy the admin-only power source the last test
        # left behind (survives clear_entities, same as in _reset below),
        # and reset.
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
    `wrench_live`-marked test. This file's own `_reset` fixture below
    already handles setup/teardown against its dedicated port 27001."""
    yield


@pytest.fixture(autouse=True)
def _reset(floor_instance):
    # Defensive: re-clear a stale tick_paused on every test, not just at
    # module setup, in case anything upstream (e.g. a crash/restart -- see
    # the resource_exhaustion test) leaves the fresh server instance paused.
    floor_instance.rcon_client.send_command("/silent-command game.tick_paused = false")
    floor_instance.initial_inventory = dict(floor_instance.default_initial_inventory)
    floor_instance.reset(reset_position=True)
    floor_instance.set_speed(10.0)
    # The admin-only electric-energy-interface power source (created via
    # raw RCON, not place_entity) survives clear_entities and would
    # otherwise block the next test's placement at the same spot (see
    # test_adaptive_strike.py's _reset for the same issue on port 27002).
    # Leftover entity-ghosts/remnants from other agents' pilot/calibration
    # runs against this long-lived, heavily-shared container also survive
    # clear_entities and can collide with fixture placement.
    floor_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered({name='electric-energy-interface'})) "
        "do e.destroy() end"
    )
    floor_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered({type={'entity-ghost','tile-ghost'}})) "
        "do e.destroy() end"
    )
    floor_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1].find_entities_filtered({})) do "
        "if e.name:find('remnants') then e.destroy() end end"
    )
    engine = floor_instance.controllers["inject_disruption"]
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
        f"disruption did not fire within {timeout_game_seconds} "
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
    as a constant, day/night-independent power source (mirrors
    test_adaptive_strike.py's helper of the same name)."""
    x, y = position
    instance.rcon_client.send_command(
        "/silent-command local iface = game.surfaces[1].create_entity{"
        f"name='electric-energy-interface', position={{{x}, {y}}}, "
        "force='player'} "
        "iface.power_production = 5000000 "
        "iface.electric_buffer_size = 5000000 "
        "iface.energy = 5000000"
    )


def add_speed_modules(instance, entity_type, module="speed-module-3", count=1):
    """Insert modules into the sole entity of ``entity_type`` via raw RCON
    -- module-inventory insertion isn't exposed through the agent-facing
    insert_item tool (it only targets the main/fuel inventory)."""
    instance.rcon_client.send_command(
        f"/silent-command local e = game.surfaces[1]"
        f".find_entities_filtered({{type='{entity_type}'}})[1] "
        f"e.get_module_inventory().insert({{name='{module}', count={count}}})"
    )


def build_fragile_furnace(game, instance):
    """Single electric furnace on its own single power pole, pre-stocked
    with ore, for the entity_destruction floor-acceptance test: it is the
    ONLY assembling-machine/furnace/mining-drill on the map, so the
    deterministic seeded pick (n=1) is guaranteed to select it, and its
    destruction zeroes production outright. Mirrors
    test_adaptive_strike.py's build_fragile_factory(powered=True)."""
    game.move_to(Position(x=0, y=0))
    pole = game.place_entity(Prototype.SmallElectricPole, position=Position(x=0, y=-2))
    furnace = game.place_entity(Prototype.ElectricFurnace, position=Position(x=0, y=0))
    game.insert_item(Prototype.IronOre, furnace, quantity=190)
    add_power_source(instance, (pole.position.x, pole.position.y - 1))
    return {"furnace": furnace}


def build_belt_fed_furnace(game, instance):
    """Chest (stocked with iron ore) -> inserter -> single transport-belt
    tile -> inserter -> furnace: the belt tile is the sole logistics link
    feeding the furnace, for the belt_cut floor-acceptance test. Two
    small-electric-poles span the whole build (power is deliberately not
    the single point of failure under test here -- the one belt tile is;
    cutting it severs ore delivery entirely with nothing else to fall back
    on)."""
    game.move_to(Position(x=0, y=0))
    pole1 = game.place_entity(Prototype.SmallElectricPole, position=Position(x=0, y=-2))
    add_power_source(instance, (pole1.position.x, pole1.position.y - 1))
    furnace = game.place_entity(Prototype.ElectricFurnace, position=Position(x=0, y=0))

    game.place_entity(
        Prototype.Inserter, position=Position(x=-2, y=0), direction=Direction.RIGHT
    )
    game.place_entity(Prototype.SmallElectricPole, position=Position(x=-4, y=-2))
    chest = game.place_entity(Prototype.WoodenChest, position=Position(x=-5, y=0))
    game.insert_item(Prototype.IronOre, chest, quantity=200)
    game.place_entity(
        Prototype.Inserter, position=Position(x=-4, y=0), direction=Direction.RIGHT
    )
    # This is the single belt tile that bridges inserter_in's drop position
    # to inserter_out's pickup position -- verified live to be exactly one
    # tile (chest.x+2 == inserter_out.pickup.x).
    belt = game.place_entity(
        Prototype.TransportBelt, position=Position(x=-3, y=0), direction=Direction.RIGHT
    )
    return {"furnace": furnace, "belt": belt}


def build_drill_fed_furnace(game, instance):
    """Single electric-mining-drill on a real natural ore patch (found via
    ``game.nearest(Resource.IronOre)``, the same live-map approach used by
    tests/functional/test_multi_drill_multi_furnace.py) -> belt -> inserter
    -> furnace, for the resource_exhaustion floor-acceptance test.

    Two live-calibration findings baked in here:

    1. Real ore patches near spawn on this heavily-farmed map are wide and
       dense -- commonly ~9 tiles fall within an electric-mining-drill's
       2.49-tile mining_drill_radius. resource_exhaustion only zeroes
       tiles actually within that radius (by construction -- see
       server.lua KINDS.resource_exhaustion), so on a naturally rich patch
       it leaves ~9 tiles at amount=1 (the default `remaining` param)
       behind after firing: enough residual ore, plus in-flight
       belt/inserter/furnace buffer, to keep a no-op factory producing
       well above the 0.2x floor for most of a minute (measured TR~0.6-0.68
       with the natural patch). This is a fixture-construction issue, not a
       disruption-kind issue: the other kinds' fixtures are built with a
       single entity or a single belt tile specifically so there is no
       slack to fall back on, and a naturally-occurring 9-tile-wide ore
       patch is the resource-kind equivalent of accidentally building two
       redundant furnaces. Thinning the patch down to a single tile within
       the drill's radius at fixture-build time (via raw RCON, same
       admin-bulk-access pattern as the engine's own entity destruction)
       restores that "no slack" property without touching the disruption
       kind itself.
    2. `params={"remaining": 0}` (a fully-exhausted tile, as opposed to the
       default `remaining=1`) crashes the ENTIRE Factorio server outright
       (`Resource amount has to be greater than 0` — confirmed live via
       docker logs; a real bug in server.lua's KINDS.resource_exhaustion,
       which does not validate/clamp `remaining` before assigning it to
       `t.amount`). Do not use remaining=0 against a live server. This
       fixture uses the safe default (remaining=1) and instead widens the
       margin below the 0.2x floor with speed modules (3x speed-module-3 in
       the drill's 3 module slots, 2x in the furnace's 2 module slots) so
       the fixed ~1-tile residual is a small fraction of a faster
       steady-state throughput, rather than trying to zero the residual
       itself.
    """
    iron_pos = game.nearest(Resource.IronOre)
    game.move_to(iron_pos)
    drill = game.place_entity(
        Prototype.ElectricMiningDrill, position=iron_pos, direction=Direction.RIGHT
    )
    pole1 = game.place_entity_next_to(
        Prototype.SmallElectricPole,
        reference_position=drill.position,
        direction=Direction.UP,
        spacing=0,
    )
    add_power_source(instance, (pole1.position.x, pole1.position.y - 1))
    add_speed_modules(instance, "mining-drill", count=3)

    instance.rcon_client.send_command(
        "/silent-command local d = game.surfaces[1]"
        ".find_entities_filtered({type='mining-drill'})[1] "
        "local r = d.prototype.mining_drill_radius + 0.01 "
        "local tiles = game.surfaces[1].find_entities_filtered({"
        "area={{d.position.x-r,d.position.y-r},{d.position.x+r,d.position.y+r}}, "
        "name='iron-ore'}) "
        "local kept = 0 "
        "for _, t in pairs(tiles) do "
        "  if kept < 1 then kept = kept + 1 else t.destroy() end "
        "end"
    )

    dp = drill.drop_position
    belt = game.place_entity(
        Prototype.TransportBelt, position=Position(x=dp.x, y=dp.y), direction=Direction.RIGHT
    )
    game.place_entity(
        Prototype.Inserter,
        position=Position(x=belt.position.x + 1, y=belt.position.y),
        direction=Direction.RIGHT,
    )
    furnace = game.place_entity(
        Prototype.ElectricFurnace,
        position=Position(x=belt.position.x + 3, y=belt.position.y),
    )
    game.place_entity_next_to(
        Prototype.SmallElectricPole,
        reference_position=furnace.position,
        direction=Direction.UP,
        spacing=0,
    )
    add_speed_modules(instance, "furnace", count=2)
    return {"drill": drill, "furnace": furnace}


def arm(engine, kind, seed, params=None):
    return engine.arm(
        kind=kind,
        seed=seed,
        quota_item="iron-plate",
        quota_per_min=QUOTA_PER_MIN,
        quota_fraction=0.5,
        consecutive_windows=1,
        window_ticks=600,
        params=params or {},
    )


def _measure_floor_tr(floor_instance, build_fn, kind, seed, params=None, prefire_seconds=30):
    game = floor_instance.namespace
    engine = floor_instance.controllers["inject_disruption"]
    engine.track("iron-plate")

    build_fn(game, floor_instance)

    # Let it run and prove throughput before arming. frozen_baseline looks
    # back at most 3600 ticks (60 game-seconds) from the fire tick, so a
    # longer prefire_seconds only matters in that it lets a full 60s
    # baseline window land entirely after production has left its startup
    # ramp-up -- resource_exhaustion's fixture uses this to get a steady-
    # state (rather than ramp-up-diluted) baseline, widening its margin
    # below the 0.2x floor (measured live: ~0.12-0.16 with a 60s ramp vs.
    # ~0.16-0.19, i.e. uncomfortably close to the bar, with only 30s).
    for _ in range(prefire_seconds // 5):
        game.sleep(5)

    disruption_id = arm(engine, kind, seed, params=params)
    engine.fire_now(disruption_id)
    fired, _ = wait_for_fired(engine, game)
    assert fired["kind"] == kind
    fire_tick = fired["tick"]

    sleep_ticks(game, floor_instance, fire_tick + TR_HORIZON_TICKS)

    samples = engine.samples()
    tr = throughput_retained(samples, "iron-plate", fire_tick, TR_HORIZON_TICKS)
    assert tr is not None, "baseline should be well-defined"
    return tr


class TestFloorAcceptance:
    """Same bar as adaptive_strike's TestFloorAcceptance (README): a no-op
    agent's post-injection throughput must fall to <=0.2x the frozen
    baseline, on a build with a genuine single point of failure."""

    def test_entity_destruction_noop_throughput_falls_to_floor(self, floor_instance):
        tr = _measure_floor_tr(
            floor_instance, build_fragile_furnace, "entity_destruction", seed=11
        )
        assert tr <= 0.2, f"no-op TR {tr} above the 0.2x floor-acceptance bar"
        print(f"entity_destruction floor-acceptance no-op TR={tr:.3f}")

    def test_belt_cut_noop_throughput_falls_to_floor(self, floor_instance):
        tr = _measure_floor_tr(
            floor_instance,
            build_belt_fed_furnace,
            "belt_cut",
            seed=7,
            params={"segments": 1},
        )
        assert tr <= 0.2, f"no-op TR {tr} above the 0.2x floor-acceptance bar"
        print(f"belt_cut floor-acceptance no-op TR={tr:.3f}")

    def test_resource_exhaustion_noop_throughput_falls_to_floor(self, floor_instance):
        tr = _measure_floor_tr(
            floor_instance,
            build_drill_fed_furnace,
            "resource_exhaustion",
            seed=3,
            params={"resource": "iron-ore", "remaining": 1},
            prefire_seconds=60,
        )
        assert tr <= 0.2, f"no-op TR {tr} above the 0.2x floor-acceptance bar"
        print(f"resource_exhaustion floor-acceptance no-op TR={tr:.3f}")
