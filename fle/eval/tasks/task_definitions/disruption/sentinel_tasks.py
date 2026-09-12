"""WRENCH sentinel task definitions.

Disruption-recovery variants of the lab-play throughput tasks: same quota
framing, but the episode is perturbed by seeded, precondition-gated
disruptions armed through the server-side WRENCH engine.

The goal descriptions tell the agent *that* disruptions can happen and which
tools to use -- never the schedule, kinds-per-task, or seeds. Those live only
here and in the ledger.
"""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from fle.disruptions import DisruptionKind, DisruptionSpec
from fle.env.game_types import Prototype
from fle.eval.tasks.scarcity_task import SCARCITY_NOTICE

# Task name constants for easy importing
IRON_PLATE_SENTINEL = "iron_plate_sentinel"
IRON_GEAR_SENTINEL = "iron_gear_sentinel"
COPPER_CABLE_SENTINEL = "copper_cable_sentinel"
IRON_PLATE_OBSERVABILITY_SENTINEL = "iron_plate_observability_sentinel"
IRON_PLATE_ADAPTIVE_SENTINEL = "iron_plate_adaptive_sentinel"
IRON_PLATE_SCARCITY_SENTINEL = "iron_plate_scarcity_sentinel"

# Appended to every sentinel goal description. Categories of trouble are fair
# game; schedules and seeds are NEVER disclosed.
DISRUPTION_NOTICE = (
    "Disruptions may damage or degrade your factory at any time. Watch for "
    "problems (get_alerts, production numbers, get_entities), report faults "
    "you find with report_fault, and repair to keep production going."
)

# Appended instead of DISRUPTION_NOTICE on budgeted tasks: same categories of
# trouble, plus an explicit heads-up that a subset of inspection tools is
# metered (get_alerts is not) -- calibrated so brute-force per-step
# get_entities sweeps run out well before the episode ends, but periodic or
# alert-triggered checks do not. See fle/eval/tasks/observability_budget.py.
OBSERVABILITY_DISRUPTION_NOTICE = (
    "Disruptions may damage or degrade your factory at any time. Watch for "
    "problems (get_alerts, production numbers, get_entities), report faults "
    "you find with report_fault, and repair to keep production going. "
    "get_entity, get_entities, and inspect_inventory calls are metered: you "
    "have a limited number for the whole episode (shown each step as "
    "'Inspection calls remaining'). Calls beyond the budget still work but "
    "count against you at scoring time, so prefer get_alerts (unmetered) "
    "and targeted checks over wide or repeated polling."
)


class DisruptionTaskConfig(BaseModel):
    """Configuration for a WRENCH disruption-recovery task."""

    task_type: Literal["disruption_recovery"] = "disruption_recovery"
    num_agents: int = 1
    trajectory_length: int = 32
    holdout_wait_period: int = 60
    pre_holdout_wait_period: int = 60
    verify_windows: int = 2

    throughput_entity: Union[str, Prototype]
    quota: int = Field(gt=0)
    goal_description: str
    task_key: str
    disruptions: List[DisruptionSpec] = Field(default_factory=list)
    # None (default) = unmetered. Set to install an ObservabilityBudget; see
    # fle/eval/tasks/observability_budget.py.
    observability_budget: Optional[int] = None

    class Config:
        frozen = True
        extra = "forbid"
        arbitrary_types_allowed = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to kwargs for DisruptionRecoveryTask (plus task_type)."""
        data = {
            "task_type": self.task_type,
            "num_agents": self.num_agents,
            "trajectory_length": self.trajectory_length,
            "holdout_wait_period": self.holdout_wait_period,
            "pre_holdout_wait_period": self.pre_holdout_wait_period,
            "verify_windows": self.verify_windows,
            "quota": self.quota,
            "goal_description": self.goal_description,
            "task_key": self.task_key,
            "observability_budget": self.observability_budget,
            # Keep DisruptionSpec instances intact -- the task constructor
            # consumes them directly.
            "disruptions": list(self.disruptions),
        }
        entity = self.throughput_entity
        if isinstance(entity, Prototype):
            entity = entity.value
            if isinstance(entity, tuple):
                entity = entity[0]
        data["throughput_entity"] = entity
        return data


class ScarcityDisruptionTaskConfig(DisruptionTaskConfig):
    """DisruptionTaskConfig variant dispatched to ScarcitySentinelTask.

    Identical shape to DisruptionTaskConfig -- ScarcitySentinelTask accepts
    the exact same constructor kwargs as DisruptionRecoveryTask and only
    overrides starting_inventory/all_technology_reserached post-construction
    (see fle/eval/tasks/scarcity_task.py). The distinct task_type literal is
    what TaskRegistry uses to route to the right task class.
    """

    task_type: Literal["scarcity_disruption_recovery"] = "scarcity_disruption_recovery"


iron_plate_sentinel = DisruptionTaskConfig(
    goal_description=(
        "Create an automatic iron-plate factory that produces 16 iron-plate "
        f"per 60 ingame seconds. {DISRUPTION_NOTICE}"
    ),
    throughput_entity=Prototype.IronPlate,
    quota=16,
    task_key=IRON_PLATE_SENTINEL,
    disruptions=[
        # No entity_name filter: agents may smelt in stone OR electric
        # furnaces (or restructure entirely), so the destruction targets any
        # furnace/drill/assembler deterministically by seed.
        DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=11),
        DisruptionSpec(
            kind=DisruptionKind.BELT_CUT,
            seed=23,
            params={"segments": 3},
        ),
    ],
)

iron_gear_sentinel = DisruptionTaskConfig(
    goal_description=(
        "Create an automatic iron-gear-wheel factory that produces 16 "
        f"iron-gear-wheel per 60 ingame seconds. {DISRUPTION_NOTICE}"
    ),
    throughput_entity=Prototype.IronGearWheel,
    quota=16,
    task_key=IRON_GEAR_SENTINEL,
    disruptions=[
        DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=5),
        DisruptionSpec(
            kind=DisruptionKind.BELT_CUT,
            seed=17,
            params={"segments": 3},
        ),
    ],
)

copper_cable_sentinel = DisruptionTaskConfig(
    goal_description=(
        "Create an automatic copper-cable factory that produces 16 "
        f"copper-cable per 60 ingame seconds. {DISRUPTION_NOTICE}"
    ),
    throughput_entity=Prototype.CopperCable,
    quota=16,
    task_key=COPPER_CABLE_SENTINEL,
    disruptions=[
        DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=7),
        DisruptionSpec(
            kind=DisruptionKind.RESOURCE_EXHAUSTION,
            seed=13,
            params={"resource": "copper-ore", "remaining": 1},
        ),
    ],
)


# Same factory/disruptions as iron_plate_sentinel, but meters
# get_entity/get_entities/inspect_inventory (see
# fle/eval/tasks/observability_budget.py). Budget calibrated empirically: a
# real Claude Sonnet pilot run against the live server (scripts/wrench_pilot,
# 12 steps, this task) used 10 metered calls -- ~0.8/step -- for normal build
# + status-check play, including get_entity calls that hit the classic
# "NoneType, entity was destroyed" pattern from docs/failure_taxonomy.md F5.
# Extrapolated to the full 32-step trajectory that is comfortably within 48
# (~1.5x headroom) for disciplined play, while a "get_entities every step
# regardless" habit (32+ calls on that pattern alone) would still burn
# through it. Slightly generous by design for a v1 task -- see the
# soft-cap rationale in observability_budget.py: an over-tight budget makes
# the task unsolvable rather than measuring monitoring discipline.
IRON_PLATE_OBSERVABILITY_BUDGET = 48

iron_plate_observability_sentinel = DisruptionTaskConfig(
    goal_description=(
        "Create an automatic iron-plate factory that produces 16 iron-plate "
        f"per 60 ingame seconds. {OBSERVABILITY_DISRUPTION_NOTICE}"
    ),
    throughput_entity=Prototype.IronPlate,
    quota=16,
    task_key=IRON_PLATE_OBSERVABILITY_SENTINEL,
    observability_budget=IRON_PLATE_OBSERVABILITY_BUDGET,
    disruptions=[
        DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=11),
        DisruptionSpec(
            kind=DisruptionKind.BELT_CUT,
            seed=23,
            params={"segments": 3},
        ),
    ],
)


iron_plate_adaptive_sentinel = DisruptionTaskConfig(
    goal_description=(
        "Create an automatic iron-plate factory that produces 16 iron-plate "
        f"per 60 ingame seconds. {DISRUPTION_NOTICE}"
    ),
    throughput_entity=Prototype.IronPlate,
    quota=16,
    task_key=IRON_PLATE_ADAPTIVE_SENTINEL,
    disruptions=[
        # Kind-agnostic fallback first (see docs/failure_taxonomy.md F11):
        # a calibration run showed the straightforward burner-tier solution
        # to this quota (drill -> furnace, no electric infrastructure at
        # all) leaves adaptive_strike with nothing to target -- it correctly
        # resolves not_applicable "no electric poles", but the task then
        # tests nothing. entity_destruction always applies (any
        # furnace/drill/assembler), so the task has teeth against every
        # build style; chaining means it fires and recovers before
        # adaptive_strike gets its turn.
        DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=37),
        # Adaptive targeting: the engine analyzes the live electric-pole
        # network and strikes whichever pole feeds the most machines, rather
        # than picking a filtered entity by seeded index. A well-designed
        # power layout (no single pole feeding multiple machines) forces the
        # engine to settle for a single-consumer pole -- no worse than
        # entity_destruction; a fragile layout (one pole feeding everything)
        # gets punished harder. Same seed -> same tie-break, same victim on
        # an identical build. Still not_applicable on a burner-only build --
        # that outcome is itself informative (design-avoidance, F8) once the
        # task is no longer relying on this as its only disruption.
        DisruptionSpec(kind=DisruptionKind.ADAPTIVE_STRIKE, seed=29),
    ],
)


# Scarcity-first variant: same target item and quota as iron_plate_sentinel,
# but the agent starts with a bare burner-tier kit (SCARCITY_STARTING_
# INVENTORY) and no completed research instead of LAB_PLAY_POPULATED_
# STARTING_INVENTORY + all_technologies_researched=True. Quota=16 is kept
# identical to iron_plate_sentinel deliberately (not lowered): calibration
# against the live server (port 27000) measured ~14-15 iron-plate/min from a
# single fed-and-fueled burner-mining-drill+stone-furnace pair and ~28-30/min
# from two pairs, so 16/60s requires genuinely running two automated pairs
# -- and losing one pair to entity_destruction drops output back below
# quota, giving a real (not free) recovery to perform. Map-gen near spawn on
# this server was confirmed live to have exactly one practically-reachable
# iron-ore patch (and one each of copper-ore/coal/stone) within ~100 tiles;
# the next-nearest iron-ore patch is ~350+ tiles away, well outside a
# reasonable walking detour within trajectory_length steps -- so "only one
# nearby patch" holds without any task-level map manipulation.
# trajectory_length=48 (vs. 32 for the populated-inventory siblings): more
# steps are budgeted because the agent must scout, mine, and hand-build its
# first automated pair before there is any baseline throughput to disrupt,
# whereas LAB_PLAY-style tasks start from an already-stocked inventory.
iron_plate_scarcity_sentinel = ScarcityDisruptionTaskConfig(
    goal_description=(
        "Create an automatic iron-plate factory that produces 16 iron-plate "
        f"per 60 ingame seconds. {SCARCITY_NOTICE} {DISRUPTION_NOTICE}"
    ),
    throughput_entity=Prototype.IronPlate,
    quota=16,
    task_key=IRON_PLATE_SCARCITY_SENTINEL,
    trajectory_length=48,
    disruptions=[
        # Same disruption kinds as the populated-inventory sentinels -- this
        # task family is about initial conditions, not new disruption
        # mechanics. Fresh seeds (unused by the other three sentinels).
        DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=29),
        DisruptionSpec(
            kind=DisruptionKind.RESOURCE_EXHAUSTION,
            seed=31,
            params={"resource": "iron-ore", "remaining": 1},
        ),
    ],
)


DISRUPTION_TASKS = {
    IRON_PLATE_SENTINEL: iron_plate_sentinel,
    IRON_GEAR_SENTINEL: iron_gear_sentinel,
    COPPER_CABLE_SENTINEL: copper_cable_sentinel,
    IRON_PLATE_OBSERVABILITY_SENTINEL: iron_plate_observability_sentinel,
    IRON_PLATE_ADAPTIVE_SENTINEL: iron_plate_adaptive_sentinel,
    IRON_PLATE_SCARCITY_SENTINEL: iron_plate_scarcity_sentinel,
}


def get_disruption_task(task_key: str) -> DisruptionTaskConfig:
    """Get a disruption task configuration by its key."""
    if task_key not in DISRUPTION_TASKS:
        raise KeyError(f"Unknown disruption task: {task_key}")
    return DISRUPTION_TASKS[task_key]


def list_disruption_tasks() -> list[str]:
    """Get a list of all available disruption task keys."""
    return list(DISRUPTION_TASKS.keys())
