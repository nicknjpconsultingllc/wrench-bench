"""WRENCH Inspect scorers over the WrenchData store.

The wrench solver drains engine production samples and ledger events into
the WrenchData store incrementally during the episode (the engine's ring
buffer only holds ~82k ticks, so an end-of-episode snapshot would lose the
frozen pre-disruption baseline). These scorers are pure readers of that
store: they re-run ``fle.disruptions.episode.episode_metrics`` (the same
function ``WrenchEpisode.finalize`` and the verifiers package use) at
scoring time.

Denominator policy (mirrors fle.disruptions.scoring): when a metric is not
scoreable for an episode (no fires, degenerate baseline), the Score value is
NaN and metadata["scoreable"] is False. Aggregation over seeds must use the
raw numerators/denominators exposed in metadata (pooled-ratio rule: sum
numerators / sum denominators), never the mean of per-episode values --
scripts/run_table.py does exactly that.
"""

import logging
import math
from typing import List

from inspect_ai.agent import AgentState
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.util import StoreModel, store_as
from pydantic import Field

from fle.disruptions.episode import episode_metrics

logger = logging.getLogger(__name__)


class WrenchData(StoreModel):
    """Store model for WRENCH episode data (samples + ledger).

    Populated incrementally by the wrench solver each step; read by the
    scorers below after the episode ends.
    """

    # Engine sample ring buffer entries, drained incrementally:
    # [{"tick": int, "counts": {item: cumulative_produced}}, ...]
    samples: List[dict] = Field(default_factory=list)
    # Full ledger contents (LedgerEntry.model_dump() dicts): armed / fired /
    # report_fault / failed / ...
    ledger_events: List[dict] = Field(default_factory=list)
    # Tracked item + quota from the DisruptionRecoveryTask.
    quota_item: str = Field(default="")
    quota: float = Field(default=0.0)
    seed_offset: int = Field(default=0)
    # Real game.tick at the end of the episode (post-fire horizons run to
    # the episode end).
    end_tick: int = Field(default=0)
    steps_completed: int = Field(default=0)
    quota_met: bool = Field(default=False)
    error: str = Field(default="")
    # Dense per-step potential-based reward-shaping signal (see
    # fle.disruptions.scoring.recovery_potential / shaped_reward_delta),
    # accumulated incrementally by WrenchEpisode.drain(). Purely additive:
    # no existing scorer reads this field, and it is never surfaced to the
    # agent. Each entry is {tick, fire_tick, phi, delta}.
    shaped_rewards: List[dict] = Field(default_factory=list)


def _metrics(data: WrenchData) -> dict:
    return episode_metrics(
        data.samples, data.ledger_events, data.quota_item, data.end_tick
    )


@scorer(metrics=[mean()])
def throughput_retained_scorer() -> Scorer:
    """Pooled Throughput-Retained over all fired disruptions in the episode.

    Value: winsorized sum(actual)/sum(expected) across fires, horizon =
    episode end. NaN when no fire was scoreable (e.g. nothing armed).
    Metadata carries the raw pooled numerator/denominator plus per-fire
    breakdowns for cross-seed pooling.

    Also carries the redundancy-floor-adjusted numerator/denominator
    (``floor_adjusted_pooled_numerator``/``_denominator``,
    ``floor_adjusted_num_fires``) alongside the plain TR numbers above --
    additive, never replacing them. See
    ``fle.disruptions.scoring.floor_adjusted_throughput_retained_parts`` for
    the metric definition; it is only defined for ``entity_destruction``
    fires carrying a ``same_type_total`` redundancy count, so fires of other
    kinds (or missing the field) simply don't contribute to this pool,
    exactly like a degenerate baseline doesn't contribute to the plain TR
    pool above.
    """

    async def score(state: AgentState, target: Target) -> Score:
        block = _metrics(store_as(WrenchData))["throughput_retained"]
        pooled = block["value"]
        num_fires = block["metadata"]["num_fires"]
        return Score(
            value=pooled if pooled is not None else float("nan"),
            answer=f"{pooled:.3f}" if pooled is not None else "unscoreable",
            explanation=(
                f"TR pooled over {num_fires} fire(s): {pooled:.3f}"
                if pooled is not None
                else f"Not scoreable: {num_fires} fire(s), no valid baseline"
            ),
            metadata=block["metadata"],
        )

    return score


@scorer(metrics=[mean()])
def recovery_scorer() -> Scorer:
    """Fraction of fired disruptions recovered before the episode end.

    A fire counts as recovered when the post-fire trailing rate reaches
    0.9x the frozen baseline for two consecutive samples within the
    remaining episode (recovery_at with budget = end_tick - fire_tick).
    Value: recovered / scoreable fires; NaN when no fire was scoreable.
    """

    async def score(state: AgentState, target: Target) -> Score:
        block = _metrics(store_as(WrenchData))["recovery"]
        rate = block["value"]
        meta = block["metadata"]
        scoreable = meta["scoreable"]
        return Score(
            value=rate if rate is not None else float("nan"),
            answer=(
                f"{meta['recovered']}/{meta['scoreable_fires']}"
                if scoreable
                else "unscoreable"
            ),
            explanation=(
                f"Recovered {meta['recovered']} of {meta['scoreable_fires']} "
                f"scoreable fire(s)"
                if scoreable
                else f"Not scoreable: {meta['num_fires']} fire(s), no valid baseline"
            ),
            metadata=meta,
        )

    return score


@scorer(metrics=[mean()])
def detection_scorer() -> Scorer:
    """Detection recall (value) plus precision/latency detail (metadata).

    Vacuous cases follow fle.disruptions.scoring.detection_metrics:
    recall=1.0 with no fires, precision=1.0 with no reports. Raw match
    counts are exposed for cross-seed pooling.
    """

    async def score(state: AgentState, target: Target) -> Score:
        block = _metrics(store_as(WrenchData))["detection"]
        meta = block["metadata"]
        return Score(
            value=meta["recall"],
            answer=f"recall={meta['recall']:.2f}",
            explanation=(
                f"Detection over {meta['num_fires']} fire(s), "
                f"{meta['num_reports']} report(s): recall={meta['recall']:.2f}, "
                f"precision_strict={meta['precision_strict']:.2f} "
                f"(loose {meta['precision']:.2f})"
            ),
            metadata=meta,
        )

    return score


def is_scoreable(value) -> bool:
    """True when a Score value is a usable number (not NaN/None)."""
    return isinstance(value, (int, float)) and not math.isnan(float(value))
