"""Detection-scoring anti-spam guard: proves report_fault spam no longer
scores the same as a single genuine detection.

Context (see wrench_core/scoring.py's DETECTION_PRECISION_FLOOR and
detection_counts/detection_metrics docstrings for the full writeup):
report_fault is a free, unmetered no-op tool (fle.env.tools.agent
.report_fault; absent from fle.eval.tasks.observability_budget
.DEFAULT_METERED_TOOLS) that costs only one of an agent's ~32-48 trajectory
steps. Before the fix in this file's companion change, detection recall/
precision were blind to REPORT VOLUME: a policy that resubmitted the same
accurate report_fault call on a cheap, fixed cadence scored bit-for-bit
identical to a single genuine, accurate report (precision=1.0, recall=1.0
either way -- confirmed live against this exact fixture during development).
The fix caps precision credit to the earliest matching report per fired
event (report volume keeps inflating the denominator, not the numerator)
and gates recall to 0.0 whenever precision falls below
DETECTION_PRECISION_FLOOR. This file proves that gate holds against a real
Factorio server, not just synthetic ledger fixtures (tests/wrench
/test_scoring.py in wrench_core covers the synthetic case).

This file connects directly to port 27001, NOT the shared tests/wrench
fixture (which owns 27000) or test_adaptive_strike.py's dedicated port
(27002) -- other agents' live suites must not be disturbed. It defines its
own skip-if-absent instance fixture and is marked `wrench_live` so
`pytest -m "not wrench_live"` actually deselects it. The parent conftest's
autouse reset fixture (which resolves `instance` against 27000) would
normally engage for any `wrench_live`-marked test; it's overridden to a
no-op below in favor of this file's own `_reset` fixture against 27001.

Port 27001 diagnostic note (see tests/wrench/test_floor_acceptance.py's
module docstring for the full writeup): this port can be left with
`game.tick_paused = true` by an earlier interrupted session, which makes
`move_to()` hang identically to a slow-chunk-generation stall but never
resolves. This file defensively clears it via raw RCON on every reset, the
same as test_floor_acceptance.py does.
"""

import socket

import pytest

from wrench_core.scoring import detection_counts, detection_metrics
from fle.env.entities import Position
from fle.env.game_types import Prototype

pytestmark = pytest.mark.wrench_live

DETECTION_RCON_PORT = 27001


def _server_listening(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def detection_instance():
    """Direct connection to the live server on RCON :27001 (shared with
    test_floor_acceptance.py -- 27000/27002 belong to other agents' live
    suites and must not be touched)."""
    if not _server_listening(DETECTION_RCON_PORT):
        pytest.skip(f"no live Factorio server on localhost:{DETECTION_RCON_PORT}")

    from fle.env import FactorioInstance

    inst = FactorioInstance(
        address="localhost",
        tcp_port=DETECTION_RCON_PORT,
        all_technologies_researched=True,
        cache_scripts=True,
        fast=True,
        inventory={
            "electric-furnace": 5,
            "small-electric-pole": 20,
            "iron-ore": 500,
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
    `wrench_live`-marked test. This file's own `_reset` fixture below
    already handles setup/teardown against its dedicated port 27001."""
    yield


@pytest.fixture(autouse=True)
def _reset(detection_instance):
    detection_instance.rcon_client.send_command(
        "/silent-command game.tick_paused = false"
    )
    detection_instance.initial_inventory = dict(
        detection_instance.default_initial_inventory
    )
    detection_instance.reset(reset_position=True)
    detection_instance.set_speed(10.0)
    # electric-energy-interface (raw-RCON admin power source) and stray
    # ghosts/remnants from other agents' runs against this long-lived,
    # heavily-shared container survive clear_entities. Same cleanup as
    # test_floor_acceptance.py / test_adaptive_strike.py.
    detection_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered({name='electric-energy-interface'})) "
        "do e.destroy() end"
    )
    detection_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered({type={'entity-ghost','tile-ghost'}})) "
        "do e.destroy() end"
    )
    detection_instance.rcon_client.send_command(
        "/silent-command for _, e in pairs(game.surfaces[1].find_entities_filtered({})) do "
        "if e.name:find('remnants') then e.destroy() end end"
    )
    engine = detection_instance.controllers["inject_disruption"]
    engine.reset_state()
    yield


def add_power_source(instance, position):
    x, y = position
    instance.rcon_client.send_command(
        "/silent-command local iface = game.surfaces[1].create_entity{"
        f"name='electric-energy-interface', position={{{x}, {y}}}, "
        "force='player'} "
        "iface.power_production = 5000000 "
        "iface.electric_buffer_size = 5000000 "
        "iface.energy = 5000000"
    )


def build_fragile_furnace(game, instance):
    """Single electric furnace on its own single power pole -- the sole
    assembling-machine/furnace/mining-drill on the map, so the deterministic
    seeded pick (n=1) is guaranteed to select it. Mirrors
    test_floor_acceptance.py's fixture of the same name."""
    game.move_to(Position(x=0, y=0))
    pole = game.place_entity(Prototype.SmallElectricPole, position=Position(x=0, y=-2))
    furnace = game.place_entity(Prototype.ElectricFurnace, position=Position(x=0, y=0))
    game.insert_item(Prototype.IronOre, furnace, quantity=190)
    add_power_source(instance, (pole.position.x, pole.position.y - 1))
    return furnace


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


def sleep_ticks(game, instance, until_tick, margin_ticks=100):
    while True:
        current = int(
            instance.rcon_client.send_command("/silent-command rcon.print(game.tick)")
        )
        if current >= until_tick + margin_ticks:
            return current
        remaining_s = (until_tick + margin_ticks - current) / 60
        game.sleep(min(10, max(1, int(remaining_s) + 1)))


def run_episode(instance, policy, furnace_pos=(0.0, 0.0)):
    """Build the fragile-furnace fixture, arm+force-fire entity_destruction,
    then run one of two scripted report_fault policies:

    - "spam": naive, cheap, systematic -- re-declare a fault at the agent's
      own known build position every ~250 ticks, no world inspection at
      all, for 10 reports. Mirrors an RL policy that discovered "just
      re-report everything I own periodically" as a cheap way to inflate
      recall.
    - "oracle": genuinely observant -- notices the real fire promptly,
      reports it once, accurately, then stops.

    Returns the raw ledger events and the fired-event dict so the caller
    can score with wrench_core.scoring.
    """
    game = instance.namespace
    engine = instance.controllers["inject_disruption"]

    build_fragile_furnace(game, instance)

    disruption_id = engine.arm(
        kind="entity_destruction",
        seed=11,
        quota_item="iron-plate",
        quota_per_min=20,
        quota_fraction=0.5,
        consecutive_windows=1,
        window_ticks=600,
    )
    engine.fire_now(disruption_id)
    fired, events = wait_for_fired(engine, game)
    fire_tick = fired["tick"]
    assert fired["kind"] == "entity_destruction"
    assert len(fired["affected"]) == 1

    if policy == "spam":
        cadence_ticks = 250
        n_reports = 10
        for i in range(n_reports):
            sleep_ticks(game, instance, fire_tick + i * cadence_ticks, margin_ticks=0)
            game.report_fault(
                Position(x=furnace_pos[0], y=furnace_pos[1]), "periodic sweep"
            )
        end_tick = fire_tick + n_reports * cadence_ticks
    elif policy == "oracle":
        sleep_ticks(game, instance, fire_tick + 60, margin_ticks=0)
        game.report_fault(
            Position(x=furnace_pos[0], y=furnace_pos[1]), "furnace destroyed"
        )
        end_tick = fire_tick + 60
    else:
        raise ValueError(policy)

    sleep_ticks(game, instance, end_tick + 50)
    events.extend(engine.drain_events())
    return events, fired


class TestDetectionAntiSpamGuard:
    """report_fault spam must not out-score (or even match) a single
    genuine, accurate detection once the diminishing-returns cap and
    precision-floor recall gate are in place."""

    def test_spam_scores_meaningfully_worse_than_a_genuine_report(
        self, detection_instance
    ):
        spam_events, spam_fire = run_episode(detection_instance, "spam")
        spam_reports = [e for e in spam_events if e["event"] == "report_fault"]
        assert len(spam_reports) == 10, "fixture should have issued 10 spam reports"

        spam_counts = detection_counts(spam_events, [spam_fire])
        spam_metrics = detection_metrics(spam_events, [spam_fire])
        print(f"spam counts={spam_counts} metrics={spam_metrics}")

        # Volume is uncapped (raw report count), credit is capped at one
        # per fired event.
        assert spam_counts["num_reports"] == 10
        assert spam_counts["matched_fires"] == 1
        assert spam_counts["matched_reports"] == 1
        # Precision collapses under 10x report volume for one credited fire.
        assert spam_metrics["precision"] == pytest.approx(0.1)
        # Below DETECTION_PRECISION_FLOOR (0.5): recall is gated to 0.0
        # despite the fire technically having been detected.
        assert spam_metrics["recall"] == 0.0

        detection_instance.reset(reset_position=True)
        # The spam episode's admin-only power source survives
        # clear_entities and would otherwise collide with the oracle
        # episode's fixture at the same pole position (see _reset above).
        detection_instance.rcon_client.send_command(
            "/silent-command for _, e in pairs(game.surfaces[1]"
            ".find_entities_filtered({name='electric-energy-interface'})) "
            "do e.destroy() end"
        )
        detection_instance.controllers["inject_disruption"].reset_state()

        oracle_events, oracle_fire = run_episode(detection_instance, "oracle")
        oracle_reports = [e for e in oracle_events if e["event"] == "report_fault"]
        assert len(oracle_reports) == 1

        oracle_metrics = detection_metrics(oracle_events, [oracle_fire])
        print(f"oracle metrics={oracle_metrics}")

        assert oracle_metrics["precision"] == 1.0
        assert oracle_metrics["recall"] == 1.0

        # The actual bracketing claim: spam must not match, let alone beat,
        # the genuinely-observant agent on either axis.
        assert spam_metrics["recall"] < oracle_metrics["recall"]
        assert spam_metrics["precision"] < oracle_metrics["precision"]
