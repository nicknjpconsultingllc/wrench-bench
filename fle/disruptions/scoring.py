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


def _redundancy_total(fire_event) -> Optional[int]:
    """Bounded-approximate same-production-line redundancy count for an
    ``entity_destruction`` fire.

    Read from the fired event's ``affected`` manifest, NOT ``detail``:
    server.lua's ``KINDS.entity_destruction`` stashes ``same_type_total`` as
    an extra field on the (sole) manifest entry it returns, following the
    same "extra detail rides along on the manifest entry" pattern
    ``resource_exhaustion`` (``tiles_changed``/``resource``) and
    ``adaptive_strike`` (``consumers_disconnected``) already use. Because
    ``affected`` is one of ``disruption_task.py``'s ``_LEDGER_TOP_LEVEL_KEYS``,
    it is never routed through the detail-draining step, so this field lands
    on ``fire_event.affected[0]``, not ``fire_event.detail``.

    This is NOT a raw "how many entities share this name anywhere on the
    map" count (an earlier version was, and it over-counted: a distant,
    unrelated same-named entity -- an abandoned early build, an
    over-provisioned spare -- inflated the count with no bearing on the
    victim's actual production line). server.lua now groups candidates by
    the stronger of two signals before counting: same
    ``electric_network_id`` as the victim when it has one (the same signal
    ``KINDS.adaptive_strike`` uses for shared power infrastructure), or same-
    name entities within a bounded radius (``REDUNDANCY_RADIUS`` in
    server.lua) when the victim is burner-tier and has no electric network.
    Neither is true functional/topological redundancy (that would require
    tracing the actual belt/inserter chain -- an open problem, see
    server.lua's comment on why ``adaptive_strike``'s graph-cut doesn't
    generalize here either); it is a bounded, honestly-approximate
    mitigation, not a perfect fix. See server.lua's ``KINDS.entity_destruction``
    for the full grouping logic.

    Returns None when the fire isn't ``entity_destruction``, there is no
    affected entry, or the field is absent (older ledger data predating this
    field, or a kind whose manifest never sets it).
    """
    if _get(fire_event, "kind") != "entity_destruction":
        return None
    affected = _get(fire_event, "affected", []) or []
    if not affected:
        return None
    total = _get(affected[0], "same_type_total")
    if total is None:
        return None
    return int(total)


def floor_adjusted_throughput_retained_parts(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    horizon_ticks: int,
    fire_event,
) -> Optional[Tuple[float, float]]:
    """Raw (actual, expected) integrals for a redundancy-floor-adjusted TR.

    Isolates the agent's own recovery contribution from passive redundancy
    that would have retained some throughput even under a fully inert
    (no-op) agent. Only defined for ``entity_destruction`` fires, where
    server.lua's victim-selection step already counts, BEFORE the kill, how
    many same-name entities plausibly shared the victim's production setup
    (``same_type_total`` on the fired event's manifest entry -- see
    ``_redundancy_total`` for the electric-network-or-bounded-radius
    grouping this count now uses, and its honestly-approximate limits).
    That count is fixed the instant the disruption fires and cannot be
    influenced by anything the agent does afterward -- the same non-
    manipulability property ``frozen_baseline`` already has with respect to
    fire time.
    This is deliberately NOT derived from observed post-fire samples: an
    earlier design that inferred the floor from where post-fire production
    plateaus was rejected because it let an agent manufacture an
    artificially low floor by self-sabotaging (e.g. deconstructing the
    survivor) right after the fire, then "recovering" from its own damage
    for free credit. Anchoring to the fixed pre-fire count closes that off
    by construction: the floor cannot move no matter what the post-fire
    samples look like.

    floor = expected * (redundancy_total - 1) / redundancy_total

    where ``expected`` is ``throughput_retained_parts``'s raw denominator
    (frozen baseline rate x horizon) and ``redundancy_total - 1`` is the
    number of same-type entities that survived the kill (exactly one is
    always destroyed by this kind). Returns ``(actual - floor, expected -
    floor)`` -- same numerator/denominator pooling contract as
    ``throughput_retained_parts`` (sum numerators / sum denominators across
    fires/episodes, never mean-of-ratios).

    Sanity check: with a single point of failure (``redundancy_total ==
    1``), ``floor == 0`` and this collapses exactly to
    ``throughput_retained_parts``'s own output -- the floor adjustment is a
    strict no-op when there was never any redundancy to begin with.

    Returns None when:
    - the fire's ``kind`` is not ``entity_destruction`` (deliberate v1 scope
      limit: ``belt_cut`` has no cheap belt-network redundancy graph,
      ``resource_exhaustion``/``adaptive_strike`` need spatial/topology
      reasoning that doesn't generalize cheaply -- see this module's other
      denominator-policy None cases for the same "don't guess, say so"
      convention),
    - ``same_type_total`` is missing from the fired event, or is < 1, or
    - the underlying ``throughput_retained_parts`` is itself None (the
      module's usual denominator policy: degenerate/unavailable frozen
      baseline).
    """
    redundancy_total = _redundancy_total(fire_event)
    if redundancy_total is None or redundancy_total < 1:
        return None
    parts = throughput_retained_parts(samples, item, fire_tick, horizon_ticks)
    if parts is None:
        return None
    actual, expected = parts
    floor = expected * (redundancy_total - 1) / redundancy_total
    return (actual - floor, expected - floor)


def floor_adjusted_throughput_retained(
    samples: Sequence[dict],
    item: str,
    fire_tick: int,
    horizon_ticks: int,
    fire_event,
) -> Optional[float]:
    """Single-episode floor-adjusted TR scalar (winsorized to [-0.5, 1.5]).

    Convenience wrapper around ``floor_adjusted_throughput_retained_parts``
    for one-off display or tests. Cross-episode/cross-fire aggregation MUST
    use ``floor_adjusted_throughput_retained_parts``' raw
    ``(actual - floor, expected - floor)`` pair and pool by summing
    numerators/denominators separately -- see that function's docstring --
    NOT by averaging this scalar across fires. Returns None under the same
    policy as ``floor_adjusted_throughput_retained_parts`` (non-
    ``entity_destruction`` kind, missing/invalid redundancy count, or a
    degenerate baseline).
    """
    parts = floor_adjusted_throughput_retained_parts(
        samples, item, fire_tick, horizon_ticks, fire_event
    )
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

    Diminishing returns on report volume, enforced via BIPARTITE matching
    between fired events and reports -- capped on BOTH sides at once:

    - Per fire: at most ONE credited report per fired event (the earliest
      still-available matching report), so resubmitting the same accurate
      report N times cannot multiply that one fire's credit.
    - Per report: once a report has been credited to some fire, it is
      removed from the candidate pool and cannot ALSO credit a different
      fire. WRENCH's factories are compact enough that a single accurate
      report can legitimately sit within ``radius`` of more than one fired
      event's affected position (e.g. ``iron_plate_sentinel`` firing both
      ``entity_destruction`` and ``belt_cut`` in one episode); without this
      side of the cap that one report would be credited to every fire it
      happens to be near, letting ``matched_reports`` exceed ``num_reports``
      and ``detection_metrics``'s ``precision`` exceed 1.0.

    Both halves of the cap are needed together: the per-fire half alone
    stops spamming one fire with duplicate reports but does nothing to stop
    one report from being reused across several DIFFERENT fires; the
    per-report half alone (without also capping at one per fire) would
    still let ten duplicate reports for the same fire each claim it. Fires
    are resolved in the order given in ``fire_events``, each taking the
    earliest-tick, not-yet-consumed report that matches it -- so an earlier
    fire has first claim on a report it shares with a later one.
    ``num_reports`` stays the true, uncapped report volume throughout.

    The loose (``radius``) and strict (``min(3.0, radius)``) passes each
    run their OWN independent bipartite consumption: a report can be
    strict-radius-close to one fire and only loose-radius-close to
    another, and each pass resolves that on its own terms, so
    ``matched_reports`` and ``matched_reports_strict`` are not required to
    agree on which report was credited to which fire.

    By construction, ``matched_reports <= min(num_fires, num_reports)`` and
    ``matched_reports_strict <= min(num_fires, num_reports)`` always hold:
    each pass increments its counter at most once per iteration of the
    ``for fire in fire_events`` loop (bounding it by ``num_fires``), and
    only when consuming a not-yet-seen report index into that pass's own
    ``consumed`` set, which can never grow past ``num_reports`` elements
    (bounding it by ``num_reports`` too).

    ``matched_fires`` is a different question -- "was this fire ever
    reported at all" for recall -- and is intentionally NOT run through the
    same consumption accounting: it counts a fire as matched whenever ANY
    report (from the full, unconsumed pool) is within ``radius``,
    regardless of whether that same report was also claimed by another
    fire's credit. A report count of 1 can therefore still leave every fire
    it is near marked as "matched" for recall purposes even though only one
    of them can walk away with the (scarcer) precision credit.

    ``latencies`` follows the credit accounting, not the recall accounting:
    an entry is only appended when a fire wins a report in the loose-radius
    consumption pass above, so ``len(latencies) <= matched_fires`` in the
    shared-report case (a fire "matched" for recall via an already-consumed
    report contributes no latency entry). This is deliberate, not a gap:
    reusing an already-consumed report's tick as a second fire's latency
    would double-count that one detection event's timing as if it
    independently explained two fires. Callers already treat ``latencies``
    as self-contained (its own ``len()`` is the denominator for any mean,
    never ``matched_fires`` -- see ``wrench_scorers.py``'s
    ``detection_scorer`` and ``run_table.py``'s aggregation), so this holds
    without requiring any downstream change.

    ``report_fault`` is a free, unmetered no-op tool (see
    ``fle.env.tools.agent.report_fault`` and
    ``fle.eval.tasks.observability_budget.DEFAULT_METERED_TOOLS``) that
    costs only one of an agent's ~32-48 trajectory steps, so without the
    per-fire cap a policy could resubmit the same accurate report, or sweep
    a small fixed set of positions it already knows about (e.g. every
    entity it has ever placed), and be scored identically to a single
    genuine detection. Measured live (tests/wrench/test_detection_gaming.py):
    10 identical, accurate report_fault calls at a 250-tick cadence against
    one fired event previously produced matched_reports=10 -- precision=1.0,
    exactly like a lone accurate report. Capping credit to the earliest
    match per fire while leaving ``num_reports`` uncapped makes report
    volume directly suppress precision (measured ~0.1 for that same
    10-report sweep post-fix).
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
    # Which report indices have already been spent as credit, tracked
    # separately per radius pass -- see the docstring's "independent
    # bipartite consumption" note.
    consumed_loose = set()
    consumed_strict = set()
    for fire in fire_events:
        fire_tick = int(_get(fire, "tick", 0))

        # Recall bookkeeping: was this fire reported at all? Deliberately
        # NOT scoped to `consumed_loose` -- see the docstring.
        matching_ticks = [
            tick for tick, pos in report_info if _matches(tick, pos, fire, radius)
        ]
        if matching_ticks:
            matched_fires += 1

        # Precision credit: at most one, and only from reports not already
        # spent on an earlier fire.
        loose_candidates = [
            (tick, idx)
            for idx, (tick, pos) in enumerate(report_info)
            if idx not in consumed_loose and _matches(tick, pos, fire, radius)
        ]
        if loose_candidates:
            earliest_tick, earliest_idx = min(loose_candidates)
            matched_reports += 1  # capped: only the earliest match is credited
            consumed_loose.add(earliest_idx)  # ...and it can't be spent again
            latencies.append(earliest_tick - fire_tick)

        # Same idea at the strict radius, with its own independent pool.
        strict_candidates = [
            (tick, idx)
            for idx, (tick, pos) in enumerate(report_info)
            if idx not in consumed_strict and _matches(tick, pos, fire, strict_radius)
        ]
        if strict_candidates:
            _, earliest_strict_idx = min(strict_candidates)
            matched_reports_strict += 1  # ditto, at the strict radius
            consumed_strict.add(earliest_strict_idx)

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
    precision_strict = c["matched_reports_strict"] / num_reports if num_reports else 1.0
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
