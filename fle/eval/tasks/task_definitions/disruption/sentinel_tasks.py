"""WRENCH sentinel task definitions.

Disruption-recovery variants of the lab-play throughput tasks: same quota
framing, but the episode is perturbed by seeded, precondition-gated
disruptions armed through the server-side WRENCH engine.

The goal descriptions tell the agent *that* disruptions can happen and which
tools to use -- never the schedule, kinds-per-task, or seeds. Those live only
here and in the ledger.
"""

from typing import Any, Dict, List, Literal, Union

from pydantic import BaseModel, Field

from fle.disruptions import DisruptionKind, DisruptionSpec
from fle.env.game_types import Prototype

# Task name constants for easy importing
IRON_PLATE_SENTINEL = "iron_plate_sentinel"
IRON_GEAR_SENTINEL = "iron_gear_sentinel"
COPPER_CABLE_SENTINEL = "copper_cable_sentinel"
IRON_PLATE_ADAPTIVE_SENTINEL = "iron_plate_adaptive_sentinel"

# Appended to every sentinel goal description. Categories of trouble are fair
# game; schedules and seeds are NEVER disclosed.
DISRUPTION_NOTICE = (
    "Disruptions may damage or degrade your factory at any time. Watch for "
    "problems (get_alerts, production numbers, get_entities), report faults "
    "you find with report_fault, and repair to keep production going."
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


iron_plate_adaptive_sentinel = DisruptionTaskConfig(
    goal_description=(
        "Create an automatic iron-plate factory that produces 16 iron-plate "
        f"per 60 ingame seconds. {DISRUPTION_NOTICE}"
    ),
    throughput_entity=Prototype.IronPlate,
    quota=16,
    task_key=IRON_PLATE_ADAPTIVE_SENTINEL,
    disruptions=[
        # Adaptive targeting: the engine analyzes the live electric-pole
        # network and strikes whichever pole feeds the most machines, rather
        # than picking a filtered entity by seeded index. A well-designed
        # power layout (no single pole feeding multiple machines) forces the
        # engine to settle for a single-consumer pole -- no worse than
        # entity_destruction; a fragile layout (one pole feeding everything)
        # gets punished harder. Same seed -> same tie-break, same victim on
        # an identical build.
        DisruptionSpec(kind=DisruptionKind.ADAPTIVE_STRIKE, seed=29),
    ],
)


DISRUPTION_TASKS = {
    IRON_PLATE_SENTINEL: iron_plate_sentinel,
    IRON_GEAR_SENTINEL: iron_gear_sentinel,
    COPPER_CABLE_SENTINEL: copper_cable_sentinel,
    IRON_PLATE_ADAPTIVE_SENTINEL: iron_plate_adaptive_sentinel,
}


def get_disruption_task(task_key: str) -> DisruptionTaskConfig:
    """Get a disruption task configuration by its key."""
    if task_key not in DISRUPTION_TASKS:
        raise KeyError(f"Unknown disruption task: {task_key}")
    return DISRUPTION_TASKS[task_key]


def list_disruption_tasks() -> list[str]:
    """Get a list of all available disruption task keys."""
    return list(DISRUPTION_TASKS.keys())
