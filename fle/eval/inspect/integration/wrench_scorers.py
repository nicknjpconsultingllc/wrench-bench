"""WRENCH Inspect scorers over the WrenchData store.

The wrench solver drains engine production samples and ledger events into
the WrenchData store incrementally during the episode (the engine's ring
buffer only holds ~82k ticks, so an end-of-episode snapshot would lose the
frozen pre-disruption baseline). These scorers are pure readers of that
store: they re-run the post-hoc functions from fle.disruptions.scoring at
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
from typing import List, Optional

from inspect_ai.agent import AgentState
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.util import StoreModel, store_as
from pydantic import Field

from fle.disruptions.scoring import (
    detection_counts,
    detection_metrics,
    frozen_baseline,
    recovery_at,
    throughput_retained_parts,
    winsorize_tr,
)

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


def _fires(data: WrenchData) -> List[dict]:
    return [e for e in data.ledger_events if e.get("event") == "fired"]


def _item(data: WrenchData) -> str:
    """Tracked item name; the engine's sample counts key is authoritative."""
    for sample in reversed(data.samples):
        counts = sample.get("counts") or {}
        if counts:
            return next(iter(counts))
    return data.quota_item


def _fire_summary(fire: dict) -> dict:
    return {"kind": fire.get("kind"), "tick": fire.get("tick"), "seed": fire.get("seed")}


@scorer(metrics=[mean()])
def throughput_retained_scorer() -> Scorer:
    """Pooled Throughput-Retained over all fired disruptions in the episode.

    Value: winsorized sum(actual)/sum(expected) across fires, horizon =
    episode end. NaN when no fire was scoreable (e.g. nothing armed).
    Metadata carries the raw pooled numerator/denominator plus per-fire
    breakdowns for cross-seed pooling.
    """

    async def score(state: AgentState, target: Target) -> Score:
        data = store_as(WrenchData)
        fires = _fires(data)
        item = _item(data)
        per_fire = []
        num = 0.0
        den = 0.0
        for fire in fires:
            fire_tick = int(fire.get("tick", 0))
            horizon = max(0, data.end_tick - fire_tick)
            parts = throughput_retained_parts(data.samples, item, fire_tick, horizon)
            entry = _fire_summary(fire)
            entry["horizon_ticks"] = horizon
            entry["baseline_per_min"] = frozen_baseline(data.samples, item, fire_tick)
            if parts is not None:
                actual, expected = parts
                entry["actual"] = actual
                entry["expected"] = expected
                entry["tr"] = winsorize_tr(actual / expected)
                num += actual
                den += expected
            else:
                entry["actual"] = None
                entry["expected"] = None
                entry["tr"] = None
            per_fire.append(entry)

        scoreable = den > 0
        pooled: Optional[float] = winsorize_tr(num / den) if scoreable else None
        return Score(
            value=pooled if pooled is not None else float("nan"),
            answer=f"{pooled:.3f}" if pooled is not None else "unscoreable",
            explanation=(
                f"TR pooled over {len(fires)} fire(s): {pooled:.3f}"
                if pooled is not None
                else f"Not scoreable: {len(fires)} fire(s), no valid baseline"
            ),
            metadata={
                "scoreable": scoreable,
                "item": item,
                "num_fires": len(fires),
                "pooled_numerator": num,
                "pooled_denominator": den,
                "fires": per_fire,
            },
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
        data = store_as(WrenchData)
        fires = _fires(data)
        item = _item(data)
        per_fire = []
        recovered = 0
        scoreable_fires = 0
        for fire in fires:
            fire_tick = int(fire.get("tick", 0))
            budget = max(0, data.end_tick - fire_tick)
            result = recovery_at(data.samples, item, fire_tick, budget)
            entry = _fire_summary(fire)
            entry["budget_ticks"] = budget
            entry["recovered"] = result
            per_fire.append(entry)
            if result is not None:
                scoreable_fires += 1
                if result:
                    recovered += 1

        scoreable = scoreable_fires > 0
        rate = recovered / scoreable_fires if scoreable else None
        return Score(
            value=rate if rate is not None else float("nan"),
            answer=f"{recovered}/{scoreable_fires}" if scoreable else "unscoreable",
            explanation=(
                f"Recovered {recovered} of {scoreable_fires} scoreable fire(s)"
                if scoreable
                else f"Not scoreable: {len(fires)} fire(s), no valid baseline"
            ),
            metadata={
                "scoreable": scoreable,
                "item": item,
                "num_fires": len(fires),
                "recovered": recovered,
                "scoreable_fires": scoreable_fires,
                "fires": per_fire,
            },
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
        data = store_as(WrenchData)
        fires = _fires(data)
        metrics = detection_metrics(data.ledger_events, fires)
        counts = detection_counts(data.ledger_events, fires)
        latencies = metrics["latencies"]
        mean_latency = sum(latencies) / len(latencies) if latencies else None
        return Score(
            value=metrics["recall"],
            answer=f"recall={metrics['recall']:.2f}",
            explanation=(
                f"Detection over {counts['num_fires']} fire(s), "
                f"{counts['num_reports']} report(s): recall={metrics['recall']:.2f}, "
                f"precision_strict={metrics['precision_strict']:.2f} "
                f"(loose {metrics['precision']:.2f})"
            ),
            metadata={
                "precision": metrics["precision"],
                "precision_strict": metrics["precision_strict"],
                "recall": metrics["recall"],
                "latencies": latencies,
                "mean_latency_ticks": mean_latency,
                **{k: counts[k] for k in (
                    "matched_reports",
                    "matched_reports_strict",
                    "num_reports",
                    "matched_fires",
                    "num_fires",
                )},
            },
        )

    return score


def is_scoreable(value) -> bool:
    """True when a Score value is a usable number (not NaN/None)."""
    return isinstance(value, (int, float)) and not math.isnan(float(value))
