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

# Below this loose-radius precision, ``detection_metrics`` gates recall to
# 0.0 (see its docstring for the full anti-spam rationale). 0.5 sits right
# on the boundary of the real-pilot double-report case exercised by
# tests/wrench/test_scoring.py::test_strict_radius_separates_nearby_from_exact
# (an agent that reports one real fault twice -- once precisely, once
# loosely -- lands at precision==0.5 exactly and is NOT gated) while
# decisively catching volume spam (measured precision ~0.1 for a 10x-report
# sweep of a single known position in
# tests/wrench/test_detection_gaming.py).
DETECTION_PRECISION_FLOOR = 0.5


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


def _first_sustained_recovery_tick(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    budget_ticks: int,
    baseline: float,
    threshold: float = 0.9,
) -> Optional[int]:
    """Real game tick of the first sample completing a 2-consecutive-window
    streak at >= ``threshold x baseline``, within
    ``[fire_tick, fire_tick + budget_ticks]``. None when no such streak
    occurs inside the budget.

    The trailing series is computed over post-fire samples only, so windows
    straddling the fire tick cannot smuggle in pre-fire production (otherwise
    every episode "recovers" at the instant of the fire). Consequently a
    budget shorter than the trailing window (~1800 ticks) trivially returns
    None. Shared by ``recovery_at`` (collapses this to a bool) and
    ``time_to_recovery_parts`` (keeps the tick, for continuous-time scoring).
    Assumes ``baseline`` has already been validated by the caller (non-None,
    >= MIN_BASELINE_RATE) -- see the module docstring's denominator policy.
    """
    target = threshold * baseline
    post_samples = [s for s in _sorted_samples(samples) if s["tick"] >= fire_tick]
    post = [
        (t, r)
        for t, r in throughput_series(post_samples, item)
        if t <= fire_tick + budget_ticks
    ]
    streak = 0
    for t, rate in post:
        streak = streak + 1 if rate >= target else 0
        if streak >= 2:
            return t
    return None


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
    This is the binary threshold-crossing question; it discards WHEN the
    crossing happened, which is the gap ``time_to_recovery_parts`` fills.
    Returns None when the frozen baseline is unavailable or near zero (see
    module docstring).
    """
    baseline = frozen_baseline(samples, item, fire_tick)
    if baseline is None or baseline < MIN_BASELINE_RATE:
        return None
    tick = _first_sustained_recovery_tick(
        samples, item, fire_tick, budget_ticks, baseline, threshold
    )
    return tick is not None


def time_to_recovery_parts(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    budget_ticks: int,
    threshold: float = 0.9,
) -> Optional[Dict]:
    """Raw (duration, event_observed) survival datum behind a continuous
    time-to-recovery metric -- the efficiency-axis analogue of
    ``detection_counts``' latencies, and the fix for the gap ``recovery_at``
    leaves: two episodes that both cross the recovery threshold are
    identical under ``recovery_at`` (both True) even if one recovered in 200
    ticks and the other used the entire budget. This function keeps the
    tick.

    Returns a dict with:
    - ``ticks``: elapsed ticks from ``fire_tick`` to the first sustained
      (2-consecutive-window) crossing of ``threshold x frozen_baseline``.
    - ``recovered``: True when that crossing happened inside the budget.
    - ``budget_ticks``: the budget passed in, echoed back for convenience.

    When ``recovered`` is False, ``ticks == budget_ticks`` -- the run never
    crossed the threshold, so the true recovery time is unknown beyond "at
    least the budget" (**right-censored at the budget**, exactly the
    Kaplan-Meier-with-censoring convention already used for detection
    latency in docs/benchmark_design.md: "Non-detections are right-censored
    -- a mean over detected-only runs is biased toward models that only
    catch easy faults"). The same bias applies here: silently dropping
    never-recovered episodes and averaging only the recovered ones would
    bias the mean toward models that only attempt easy recoveries. Pool
    ``(ticks, recovered)`` pairs across fires/episodes with a proper
    survival estimator (Kaplan-Meier / restricted-mean-time-to-recovery)
    rather than averaging ``ticks`` directly -- that average is exactly the
    "bare median/mean over observed-only" bias this docstring warns against.

    Returns None under the module's denominator policy: a degenerate or
    unavailable frozen baseline is "not scoreable" (a broken episode). Do
    not confuse this with ``recovered=False``, which is a real, meaningful
    outcome (recovery genuinely did not happen within budget), not a broken
    episode.
    """
    baseline = frozen_baseline(samples, item, fire_tick)
    if baseline is None or baseline < MIN_BASELINE_RATE:
        return None
    tick = _first_sustained_recovery_tick(
        samples, item, fire_tick, budget_ticks, baseline, threshold
    )
    if tick is None:
        return {
            "ticks": float(budget_ticks),
            "recovered": False,
            "budget_ticks": budget_ticks,
        }
    return {
        "ticks": float(tick - fire_tick),
        "recovered": True,
        "budget_ticks": budget_ticks,
    }


def time_to_recovery(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    budget_ticks: int,
    threshold: float = 0.9,
) -> Optional[float]:
    """Single-episode ticks-to-recovery scalar (budget_ticks when censored).

    Convenience wrapper around ``time_to_recovery_parts`` for one-off
    display or tests. Cross-episode aggregation (Kaplan-Meier, restricted
    mean time-to-recovery) MUST use ``time_to_recovery_parts``'
    ``(ticks, recovered)`` pairs instead of collapsing to this scalar and
    averaging -- see that function's docstring for why (right-censoring
    bias). Returns None under the same denominator policy as
    ``time_to_recovery_parts``.
    """
    parts = time_to_recovery_parts(samples, item, fire_tick, budget_ticks, threshold)
    if parts is None:
        return None
    return parts["ticks"]


def recovery_potential(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    tick: int,
    window_ticks: int = 1800,
) -> Optional[float]:
    """Phi(tick): potential-based recovery signal for RL reward shaping.

    Phi(tick) = clip(trailing_rate(tick) / frozen_baseline(fire_tick), 0, 1)

    This is a Ng, Harada & Russell (1999) potential function, not a raw
    slope bonus: summing ``shaped_reward_delta`` over any span telescopes to
    ``Phi(end) - Phi(start)``, a single bounded number regardless of how
    many steps the span was cut into. That makes "creep the trailing rate up
    by an imperceptible epsilon every window, forever, banking tiny positive
    rewards without ever meaningfully recovering" structurally impossible --
    there is no way to earn more total reward by taking smaller steps.

    ``trailing_rate`` is ``throughput_series`` restricted to samples at or
    after ``fire_tick`` -- the same restriction ``_first_sustained_recovery_tick``
    applies, for the same reason: a window straddling the fire would still be
    full of pre-disruption production immediately after the fire, and every
    episode would look instantly "recovered". Before a full ``window_ticks``
    of post-fire samples exists, the trailing rate is treated as 0 -- there
    has not been time to rebuild anything yet, which is exactly the
    immediate-post-fire floor this function already clips to, so this is a
    default, not a special case. (It also means ``Phi(fire_tick) == 0``
    always, by construction.)

    Phi is NOT a running maximum -- it tracks the CURRENT trailing rate at
    ``tick``, so backsliding after partial recovery correctly lowers Phi
    (and, through ``shaped_reward_delta``, produces a negative reward that
    cancels prior credit). A ratchet/running-max version would reopen a
    version of the same exploit this function is meant to close: touch the
    baseline once, keep the credit forever.

    Returns None when ``tick`` is before ``fire_tick`` (Phi is only defined
    post-fire) or when the frozen baseline is unavailable or near zero (<
    ``MIN_BASELINE_RATE``) -- see the module docstring's denominator policy.
    Callers must treat None as not-scoreable, never coerce to 0.
    """
    if tick < fire_tick:
        return None
    baseline = frozen_baseline(samples, item, fire_tick)
    if baseline is None or baseline < MIN_BASELINE_RATE:
        return None
    ordered = _sorted_samples(samples)
    post = [s for s in ordered if s["tick"] >= fire_tick]
    series = [
        (t, rate)
        for t, rate in throughput_series(post, item, window_ticks)
        if t <= tick
    ]
    rate = series[-1][1] if series else 0.0
    return max(0.0, min(1.0, rate / baseline))


def shaped_reward_delta(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    prev_tick: int,
    tick: int,
    window_ticks: int = 1800,
) -> Optional[float]:
    """r_shaped(t) = Phi(tick) - Phi(prev_tick).

    The per-step potential-based reward-shaping term (Ng, Harada & Russell
    1999) for the span ``[prev_tick, tick]`` against ``fire_tick``'s frozen
    baseline. See ``recovery_potential`` for Phi's definition and the
    telescoping property that makes this gaming-resistant: summed over any
    sequence of consecutive (prev_tick, tick) pairs spanning
    ``[fire_tick, T]``, this collapses to ``Phi(T) - Phi(fire_tick)`` --
    i.e. ``Phi(T)``, since ``Phi(fire_tick) == 0`` by construction --
    regardless of step granularity.

    Propagates None if either endpoint is unscoreable (degenerate baseline,
    or a tick before ``fire_tick``) -- callers must treat None as
    not-scoreable, never coerce to 0 (a real 0.0 delta and an unscoreable
    episode are different things).
    """
    phi_tick = recovery_potential(samples, item, fire_tick, tick, window_ticks)
    phi_prev = recovery_potential(samples, item, fire_tick, prev_tick, window_ticks)
    if phi_tick is None or phi_prev is None:
        return None
    return phi_tick - phi_prev


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

    Diminishing returns on report volume: ``matched_reports`` and
    ``matched_reports_strict`` are CREDITED match counts, capped at ONE
    credit per fired event (the earliest matching report), not a raw count
    of every report that happens to match. ``num_reports`` stays the true,
    uncapped report volume. ``report_fault`` is a free, unmetered no-op
    tool (see ``fle.env.tools.agent.report_fault`` and
    ``fle.eval.tasks.observability_budget.DEFAULT_METERED_TOOLS``) that
    costs only one of an agent's ~32-48 trajectory steps, so without this
    cap a policy could resubmit the same accurate report, or sweep a small
    fixed set of positions it already knows about (e.g. every entity it has
    ever placed), and be scored identically to a single genuine detection.
    Measured live (tests/wrench/test_detection_gaming.py): 10 identical,
    accurate report_fault calls at a 250-tick cadence against one fired
    event previously produced matched_reports=10 -- precision=1.0, exactly
    like a lone accurate report. Capping credit to the earliest match per
    fire while leaving ``num_reports`` uncapped makes report volume
    directly suppress precision (measured ~0.1 for that same 10-report
    sweep post-fix).
    """
    reports = [e for e in ledger_entries if _get(e, "event") == "report_fault"]
    report_info = [(int(_get(r, "tick", 0)), _report_position(r)) for r in reports]
    # strict variant: a 3-tile radius separates "named the damaged entity"
    # from "reported something nearby" (the loose 10-tile radius credited a
    # full-chest report 6.7 tiles from a destroyed drill in the first pilot)
    strict_radius = min(3.0, radius)

    latencies: List[int] = []
    matched_fires = 0
    matched_reports = 0
    matched_reports_strict = 0
    for fire in fire_events:
        fire_tick = int(_get(fire, "tick", 0))
        matching_ticks = [
            tick
            for tick, pos in report_info
            if _matches(tick, pos, fire, radius)
        ]
        if matching_ticks:
            matched_fires += 1
            matched_reports += 1  # capped: only the earliest match is credited
            latencies.append(min(matching_ticks) - fire_tick)
        if any(_matches(tick, pos, fire, strict_radius) for tick, pos in report_info):
            matched_reports_strict += 1  # ditto, at the strict radius

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
    precision_floor: float = DETECTION_PRECISION_FLOOR,
) -> Dict:
    """Detection latency/precision/recall from report_fault vs fired events.

    A report matches a fired event when its (x, y) is within ``radius`` of
    any affected entity of that event and its tick is >= the fire tick.

    - ``latencies``: for each fired event (in order), the tick delta to the
      FIRST matching report; events never matched contribute no latency.
    - ``precision`` / ``precision_strict``: credited matches (at most one
      per fired event -- see ``detection_counts``) over raw report volume.
      1.0 when there are no reports (vacuously no false positives).
    - ``recall``: fraction of fired events ever matched, GATED to 0.0
      whenever ``precision`` (the loose-radius one) falls below
      ``precision_floor`` and there is at least one fired event. 1.0 when
      there are no fired events (vacuous -- there is nothing for the gate
      to withhold).

    The recall gate is the anti-spam guard, not just the volume-sensitive
    precision above it: ``fle.eval.inspect.integration.wrench_scorers
    .detection_scorer`` reports ``recall`` itself as the scored Inspect
    ``Score.value`` (precision only ever reaches ``metadata``, which no
    aggregator currently reads), so a policy that is mostly noise but
    happens to land one lucky/redundant hit must not still walk away with
    recall=1.0 -- it has to actually be precise enough to earn credit for
    "found it". See ``DETECTION_PRECISION_FLOOR`` for why 0.5 was chosen.

    Use ``detection_counts`` when you need the raw numerators/denominators
    for cross-episode pooling (pooled precision/recall should be computed
    from summed counts, not by averaging this gate's already-gated output).
    """
    c = detection_counts(ledger_entries, fire_events, radius)
    num_reports = c["num_reports"]
    precision = c["matched_reports"] / num_reports if num_reports else 1.0
    precision_strict = (
        c["matched_reports_strict"] / num_reports if num_reports else 1.0
    )
    recall = c["matched_fires"] / c["num_fires"] if c["num_fires"] else 1.0
    if c["num_fires"] and precision < precision_floor:
        recall = 0.0
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
