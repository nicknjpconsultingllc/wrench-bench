"""Post-hoc WRENCH scorers over (ledger, samples).

Pure functions; no server required. Inputs:

- ``samples``: the engine's sample ring buffer as returned by
  ``inject_disruption.samples()`` -- a list of
  ``{"tick": int, "counts": {item: cumulative_produced}}`` dicts at ~41-tick
  intervals. Functions sort defensively by tick.
- ``ledger_entries`` / ``fire_events``: LedgerEntry objects or equivalent
  dicts (both attribute and key access are supported).

Denominator policy (documented once, applied everywhere): when the frozen
pre-disruption baseline cannot be computed (fewer than two samples in the
baseline window) or is near zero (< ``MIN_BASELINE_RATE`` items/min), ratio
scorers return ``None`` rather than dividing by ~0. Callers should treat
``None`` as "not scoreable", never as 0. This case is bounded away by
construction in real episodes -- disruptions arm on a demonstrated-throughput
precondition -- so ``None`` signals a broken episode, not a bad agent.
"""

from typing import Dict, List, Optional, Sequence, Tuple

# Below this trailing rate (items/min) a baseline is considered degenerate.
MIN_BASELINE_RATE = 1e-6

TICKS_PER_MINUTE = 3600


def _get(obj, key, default=None):
    """Read `key` from a dict or an attribute from an object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _sorted_samples(samples: Sequence[dict]) -> List[dict]:
    return sorted(samples, key=lambda s: s["tick"])


def _count(sample: dict, item: str) -> float:
    return float(sample.get("counts", {}).get(item, 0))


def _count_at(samples: List[dict], item: str, tick: float) -> Optional[float]:
    """Cumulative production at `tick`, linearly interpolated between the
    bracketing samples. Clamps to the first/last sample outside the range.
    Returns None when there are no samples."""
    if not samples:
        return None
    if tick <= samples[0]["tick"]:
        return _count(samples[0], item)
    if tick >= samples[-1]["tick"]:
        return _count(samples[-1], item)
    for prev, nxt in zip(samples, samples[1:]):
        if prev["tick"] <= tick <= nxt["tick"]:
            span = nxt["tick"] - prev["tick"]
            if span <= 0:
                return _count(nxt, item)
            frac = (tick - prev["tick"]) / span
            c0, c1 = _count(prev, item), _count(nxt, item)
            return c0 + frac * (c1 - c0)
    return _count(samples[-1], item)  # unreachable given sort, defensive


def throughput_series(
    samples: Sequence[dict], item: str, window_ticks: int = 1800
) -> List[Tuple[int, float]]:
    """Trailing-window production rate at each sample tick.

    For each sample, the rate is the finite difference against the most
    recent sample at least ``window_ticks`` older, scaled to items/min
    (mirrors the Lua engine's ``trailing_rate``). Samples too early to have
    a full trailing window are omitted.

    Returns a list of ``(tick, rate_per_min)`` tuples.
    """
    ordered = _sorted_samples(samples)
    series: List[Tuple[int, float]] = []
    for i, newest in enumerate(ordered):
        base = None
        for j in range(i - 1, -1, -1):
            if newest["tick"] - ordered[j]["tick"] >= window_ticks:
                base = ordered[j]
                break
        if base is None:
            continue
        dt = newest["tick"] - base["tick"]
        if dt <= 0:
            continue
        produced = _count(newest, item) - _count(base, item)
        series.append((newest["tick"], produced * TICKS_PER_MINUTE / dt))
    return series


def frozen_baseline(
    samples: Sequence[dict], item: str, fire_tick: int, window_ticks: int = 3600
) -> Optional[float]:
    """Mean production rate (items/min) over the window ENDING at fire_tick.

    This is the frozen pre-disruption ceiling: it never changes after the
    disruption fires, so post-fire behavior cannot move its own denominator.
    Computed as a pooled rate between the first and last samples inside
    ``[fire_tick - window_ticks, fire_tick]`` (uses whatever portion of the
    window has samples). Returns None when fewer than two samples fall in
    the window.
    """
    ordered = _sorted_samples(samples)
    in_window = [
        s for s in ordered if fire_tick - window_ticks <= s["tick"] <= fire_tick
    ]
    if len(in_window) < 2:
        return None
    dt = in_window[-1]["tick"] - in_window[0]["tick"]
    if dt <= 0:
        return None
    produced = _count(in_window[-1], item) - _count(in_window[0], item)
    return produced * TICKS_PER_MINUTE / dt


def throughput_retained_parts(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    horizon_ticks: int,
) -> Optional[Tuple[float, float]]:
    """Raw (actual, expected) production integrals behind throughput_retained.

    ``actual`` is the production over ``[fire_tick, fire_tick + horizon]``;
    ``expected`` is ``frozen_baseline rate x horizon``. Un-winsorized, so
    callers aggregating across fires/seeds can pool correctly (sum of
    numerators / sum of denominators) instead of averaging ratios. Returns
    None under the same denominator policy as ``throughput_retained``.
    """
    baseline = frozen_baseline(samples, item, fire_tick)
    if baseline is None or baseline < MIN_BASELINE_RATE:
        return None
    ordered = _sorted_samples(samples)
    c0 = _count_at(ordered, item, fire_tick)
    c1 = _count_at(ordered, item, fire_tick + horizon_ticks)
    if c0 is None or c1 is None:
        return None
    expected = baseline * horizon_ticks / TICKS_PER_MINUTE
    if expected <= 0:
        return None
    return (c1 - c0, expected)


def winsorize_tr(ratio: float) -> float:
    """Clamp a TR ratio to [-0.5, 1.5] (see throughput_retained)."""
    return max(-0.5, min(1.5, ratio))


def throughput_retained(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    horizon_ticks: int,
) -> Optional[float]:
    """Pooled Throughput-Retained ratio over a post-fire horizon.

    TR = (actual production integral over [fire_tick, fire_tick + horizon])
         / (frozen_baseline rate x horizon).

    Winsorized to [-0.5, 1.5] so pathological curves (counter resets,
    overshoot) cannot dominate an aggregate. Returns None when the frozen
    baseline is unavailable or near zero (< MIN_BASELINE_RATE items/min) --
    see the module docstring for the denominator policy. Use
    ``throughput_retained_parts`` when you need the raw integrals for
    cross-episode pooling.
    """
    parts = throughput_retained_parts(samples, item, fire_tick, horizon_ticks)
    if parts is None:
        return None
    actual, expected = parts
    return winsorize_tr(actual / expected)


def recovery_at(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    budget_ticks: int,
    threshold: float = 0.9,
) -> Optional[bool]:
    """Did the trailing rate recover within the budget?

    True when the trailing rate reaches ``threshold x frozen_baseline`` at
    two consecutive samples inside ``[fire_tick, fire_tick + budget_ticks]``.
    The trailing series is computed over post-fire samples only, so windows
    straddling the fire tick cannot smuggle in pre-fire production (otherwise
    every episode "recovers" at the instant of the fire). Consequently a
    budget shorter than the trailing window (~1800 ticks) trivially returns
    False. Returns None when the frozen baseline is unavailable or near zero
    (see module docstring).
    """
    baseline = frozen_baseline(samples, item, fire_tick)
    if baseline is None or baseline < MIN_BASELINE_RATE:
        return None
    target = threshold * baseline
    post_samples = [s for s in _sorted_samples(samples) if s["tick"] >= fire_tick]
    post = [
        (t, r)
        for t, r in throughput_series(post_samples, item)
        if t <= fire_tick + budget_ticks
    ]
    streak = 0
    for _, rate in post:
        streak = streak + 1 if rate >= target else 0
        if streak >= 2:
            return True
    return False


def _affected_positions(fire_event) -> List[Tuple[float, float]]:
    positions = []
    for entry in _get(fire_event, "affected", []) or []:
        x, y = _get(entry, "x"), _get(entry, "y")
        if x is not None and y is not None:
            positions.append((float(x), float(y)))
    return positions


def _report_position(entry) -> Optional[Tuple[float, float]]:
    detail = _get(entry, "detail", {}) or {}
    x = _get(detail, "x", _get(entry, "x"))
    y = _get(detail, "y", _get(entry, "y"))
    if x is None or y is None:
        return None
    return (float(x), float(y))


def _matches(report_tick, report_pos, fire_event, radius) -> bool:
    fire_tick = _get(fire_event, "tick", 0)
    if report_tick < fire_tick:
        return False
    if report_pos is None:
        return False
    r2 = radius * radius
    for ax, ay in _affected_positions(fire_event):
        dx, dy = report_pos[0] - ax, report_pos[1] - ay
        if dx * dx + dy * dy <= r2:
            return True
    return False


def detection_counts(
    ledger_entries: Sequence,
    fire_events: Sequence,
    radius: float = 10.0,
) -> Dict:
    """Raw match counts behind ``detection_metrics``.

    Returns latencies plus the integer numerators/denominators
    (``matched_reports[_strict]`` / ``num_reports``, ``matched_fires`` /
    ``num_fires``) so callers can pool detection precision/recall across
    episodes and seeds (sum numerators / sum denominators) instead of
    averaging per-episode ratios.
    """
    reports = [e for e in ledger_entries if _get(e, "event") == "report_fault"]
    report_info = [(int(_get(r, "tick", 0)), _report_position(r)) for r in reports]

    latencies: List[int] = []
    matched_fires = 0
    for fire in fire_events:
        fire_tick = int(_get(fire, "tick", 0))
        matching_ticks = [
            tick
            for tick, pos in report_info
            if _matches(tick, pos, fire, radius)
        ]
        if matching_ticks:
            matched_fires += 1
            latencies.append(min(matching_ticks) - fire_tick)

    matched_reports = sum(
        1
        for tick, pos in report_info
        if any(_matches(tick, pos, fire, radius) for fire in fire_events)
    )
    # strict variant: a 3-tile radius separates "named the damaged entity"
    # from "reported something nearby" (the loose 10-tile radius credited a
    # full-chest report 6.7 tiles from a destroyed drill in the first pilot)
    strict_radius = min(3.0, radius)
    matched_reports_strict = sum(
        1
        for tick, pos in report_info
        if any(_matches(tick, pos, fire, strict_radius) for fire in fire_events)
    )
    return {
        "latencies": latencies,
        "matched_reports": matched_reports,
        "matched_reports_strict": matched_reports_strict,
        "num_reports": len(reports),
        "matched_fires": matched_fires,
        "num_fires": len(fire_events),
    }


def detection_metrics(
    ledger_entries: Sequence,
    fire_events: Sequence,
    radius: float = 10.0,
) -> Dict:
    """Detection latency/precision/recall from report_fault vs fired events.

    A report matches a fired event when its (x, y) is within ``radius`` of
    any affected entity of that event and its tick is >= the fire tick.

    - ``latencies``: for each fired event (in order), the tick delta to the
      FIRST matching report; events never matched contribute no latency.
    - ``precision``: fraction of report_fault entries matching some fired
      event. 1.0 when there are no reports (vacuously no false positives).
    - ``recall``: fraction of fired events ever matched. 1.0 when there are
      no fired events.

    Use ``detection_counts`` when you need the raw numerators/denominators
    for cross-episode pooling.
    """
    c = detection_counts(ledger_entries, fire_events, radius)
    num_reports = c["num_reports"]
    precision = c["matched_reports"] / num_reports if num_reports else 1.0
    precision_strict = (
        c["matched_reports_strict"] / num_reports if num_reports else 1.0
    )
    recall = c["matched_fires"] / c["num_fires"] if c["num_fires"] else 1.0
    return {
        "latencies": c["latencies"],
        "precision": precision,
        "precision_strict": precision_strict,
        "recall": recall,
    }


def observability_budget_metrics(meta: Dict) -> Optional[Dict]:
    """Inspection-call budget usage from a task's final ``meta`` dict.

    Reads the keys ``ObservabilityBudget.summary()`` writes into
    ``TaskResponse.meta`` (see ``fle.eval.tasks.observability_budget`` for
    the counting mechanism and which tools are metered). Returns ``None``
    when the episode was not budgeted -- an unmetered task's meta simply
    lacks these keys, which is the expected common case, not a broken
    episode, so this does not follow the ratio-scorer "None means
    degenerate" convention above; it means "not applicable."
    """
    used = meta.get("inspection_calls_used")
    budget = meta.get("inspection_calls_budget")
    if used is None or budget is None:
        return None
    over = meta.get("inspection_calls_over_budget", max(0, used - budget))
    return {
        "calls_used": used,
        "calls_budget": budget,
        "calls_over_budget": over,
        "within_budget": used <= budget,
        "utilization": (used / budget) if budget else None,
    }
