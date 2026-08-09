"""WRENCH scarcity-first disruption-recovery task.

The existing sentinel tasks (``fle/eval/tasks/task_definitions/disruption/
sentinel_tasks.py``) start the agent with ``LAB_PLAY_POPULATED_STARTING_
INVENTORY`` (50 electric-mining-drills, 2 boilers, 10 electric-furnaces, ...)
and ``all_technologies_researched=True``. That is deliberate for the
throughput-focused tasks it was designed for, but it means a disruption's
"replace the destroyed thing" recovery path is nearly free -- the spare is
already in the inventory and the tech to rebuild it electrically is already
unlocked. Nothing about the agent's *situation* is actually scarce.

``ScarcitySentinelTask`` flips that: the agent starts with a bare burner-tier
bootstrap kit, no completed research, and must mine/smelt/build its own way
to an automatic factory before any disruption even has a baseline to damage.
Disruptions (entity_destruction, resource_exhaustion, ...) are unchanged --
this module is about *initial conditions*, not new disruption mechanics.

Design decisions, and their tradeoffs, are documented inline below.
"""

from typing import Dict

from fle.eval.tasks.disruption_task import DisruptionRecoveryTask

# ---------------------------------------------------------------------------
# Starting inventory
# ---------------------------------------------------------------------------
# Calibrated empirically against the live WRENCH server (port 27000, Factorio
# 2.0.73) rather than guessed. With all_technologies_researched=False, every
# item below is craftable/placeable from the game's default-enabled recipe
# set -- burner-mining-drill, stone-furnace, burner-inserter, transport-belt
# and wooden-chest all show recipes[...].enabled == True with *zero*
# technologies researched (confirmed live; see calibration notes in the
# iron_plate_scarcity_sentinel goal/task below). No electricity, no assembling
# machines, nothing beyond burner tier is included on purpose: the point is
# that the agent must bootstrap automation from raw resources and whatever
# minimal kit it is handed, not restock from a warehouse.
#
# Quantities: a single burner-mining-drill + stone-furnace pair, fed and
# fueled, sustains ~14-15 iron-plate/min on this map (measured live). Two
# pairs sustain ~28-30/min. The kit below gives exactly enough for two full
# pairs (the minimum that clears a 16/60s quota) plus two spares of each --
# enough to rebuild once after a single entity_destruction hit without a
# resupply run, but not the 50-drill/troops of spares LAB_PLAY hands out.
# burner-inserters: two per pair (ore-in, plate-out) plus spares.
# transport-belt: a modest length for connecting drill drop positions to
# furnaces/chests, not the 500 LAB_PLAY grants.
# coal: enough to bootstrap ~4-5 minutes of fueled operation across four
# burner entities before the agent must secure its own fuel supply from the
# (also singular, but conveniently close) nearby coal patch.
SCARCITY_STARTING_INVENTORY: Dict[str, int] = {
    "burner-mining-drill": 4,
    "stone-furnace": 4,
    "burner-inserter": 8,
    "transport-belt": 50,
    "wooden-chest": 2,
    "coal": 50,
}

# Appended to scarcity-variant goal descriptions. Explains the *framing*
# (start poor, resources may be tighter than usual) without hinting at exact
# amounts, locations, or which/when disruptions fire -- consistent with the
# "never disclose schedule or seeds" convention in sentinel_tasks.py.
SCARCITY_NOTICE = (
    "You are starting with a minimal burner-tier kit and no completed "
    "research -- no boilers, no electric drills, no pre-unlocked "
    "technology. Raw resources reachable on foot from your start may be "
    "more limited than in a typical scenario, so scout carefully, mine and "
    "smelt your own supply chain, and don't assume a spare is always "
    "sitting in inventory."
)


class ScarcitySentinelTask(DisruptionRecoveryTask):
    """A DisruptionRecoveryTask that starts the agent poor instead of rich.

    Reuses DisruptionRecoveryTask/ThroughputTask verbatim for setup/verify/
    disruption-arming -- only the starting conditions differ. ThroughputTask
    hardcodes LAB_PLAY_POPULATED_STARTING_INVENTORY and
    all_technology_reserached=True in its own __init__ (it does not expose
    either as a constructor parameter), so both are overridden here as plain
    post-construction attribute assignment: TaskABC.setup() reads
    self.starting_inventory / self.all_technology_reserached at setup() time,
    not at __init__ time, so this is safe and does not require touching the
    base classes.

    Design call -- all_technology_reserached=False (not a partial tech set):
    for the iron-plate line specifically, every recipe needed (stone-furnace,
    burner-mining-drill, burner-inserter, transport-belt, wooden-chest) is
    already enabled by the game's default recipe set with *zero* research
    completed (confirmed live against port 27000). So "no research required"
    and "no research done" coincide for this task, and False is both the
    honest and the simplest choice -- no need to special-case anything in
    setup_instance.

    This does NOT generalize for free to higher-tier target items. Live
    investigation while calibrating this task found that with
    all_technologies_researched=False, "automation" (assembling-machine-1)
    and "electronics" (copper-cable, inserter, lab, small-electric-pole) are
    both real research gates, and the "automation-science-pack" technology
    the engine's reset.lua force-completes as a bootstrap has *its own*
    prerequisites of "steam-power" and "electronics" -- neither of which is
    force-completed. A scarcity task targeting copper-cable or
    iron-gear-wheel (which needs assembling-machine-1) under
    all_technologies_researched=False would hit that chain and need either
    an explicit setup_instance override that force-researches the specific
    prerequisite techs (mirroring the reset.lua asp_tech.researched = true
    pattern) or a much longer trajectory_length to research through the lab
    normally once electricity exists. Left as future work; out of scope for
    the iron-plate variant here.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.starting_inventory = dict(SCARCITY_STARTING_INVENTORY)
        self.all_technology_reserached = False
