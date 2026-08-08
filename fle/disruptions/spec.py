"""Disruption specifications.

A DisruptionSpec describes one seeded fault to inject into a running episode.
Specs are armed on a *precondition* (measured factory throughput), never on a
bare tick: this bounds the Throughput-Retained denominator away from zero by
construction. Once armed, the server-side Lua scheduler fires the disruption
at the next sample tick and records the actual game.tick in the ledger.
"""

from enum import Enum

from pydantic import BaseModel, Field


class DisruptionKind(str, Enum):
    # v1: non-pre-emptable, permanent kinds (must pass the floor acceptance
    # test: no-op agent's post-injection throughput <= 0.2x the frozen
    # pre-disruption rate over the measurement window).
    ENTITY_DESTRUCTION = "entity_destruction"
    BELT_CUT = "belt_cut"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    # Adaptive targeting: analyzes the live build (electric-pole network
    # load) instead of picking from a filtered list by seeded index. Held to
    # the same floor acceptance test; see server.lua KINDS.adaptive_strike
    # for the heuristic and its rationale.
    ADAPTIVE_STRIKE = "adaptive_strike"
    # later families (not v1): power_loss self-heals, biter_raid is only
    # statistically deterministic.
    POWER_LOSS = "power_loss"
    BITER_RAID = "biter_raid"


class Precondition(BaseModel, frozen=True, extra="forbid"):
    """Arm the disruption only once the factory demonstrably works."""

    # Trailing throughput must reach this fraction of the task quota...
    # Default 1.0: arming below full quota lets the fire land mid-ramp,
    # where the frozen baseline underestimates the counterfactual ceiling
    # and inflates TR (observed in the first Sonnet pilot: baseline 9.1/min
    # on a factory that reached 38/min).
    quota_fraction: float = Field(default=1.0, gt=0, le=1)
    # ...for this many consecutive sample windows before the spec arms.
    consecutive_windows: int = Field(default=2, ge=1)


class DisruptionSpec(BaseModel, frozen=True, extra="forbid"):
    kind: DisruptionKind
    precondition: Precondition = Precondition()
    # Deterministic target/parameter selection within the kind
    # (e.g. which belt segment run, which assembler, patch amount).
    seed: int
    # Kind-specific parameters, validated by the library at injection time
    # (e.g. {"segments": 3} for belt_cut).
    params: dict[str, int | float | str] = Field(default_factory=dict)
    # Ticks after arming before the scheduler fires (0 = next sample tick).
    delay_ticks: int = Field(default=0, ge=0)
