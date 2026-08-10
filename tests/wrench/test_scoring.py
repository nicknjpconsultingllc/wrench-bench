"""Unit tests for the WRENCH post-hoc scorers. Pure Python; no server."""

import pytest

from fle.disruptions.ledger import LedgerEntry
from fle.disruptions.scoring import (
    detection_counts,
    detection_metrics,
    floor_adjusted_throughput_retained,
    floor_adjusted_throughput_retained_parts,
    frozen_baseline,
    recovery_at,
    recovery_potential,
    shaped_reward_delta,
    throughput_retained,
    throughput_retained_parts,
    throughput_series,
    time_to_recovery,
    time_to_recovery_parts,
)

INTERVAL = 41  # engine sample interval in ticks
FIRE_TICK = 6027  # a multiple of 41, mid-run


def make_samples(segments, item="iron-plate", start_tick=0):
    """Build a synthetic 41-tick sample ring buffer.

    segments: list of (duration_ticks, rate_per_min); cumulative production
    accrues piecewise-linearly at each segment's rate.
    """
    samples = []
    tick = start_tick
    count = 0.0
    for duration, rate in segments:
        end = tick + duration
        while tick <= end:
            samples.append({"tick": tick, "counts": {item: count}})
            step = min(INTERVAL, end - tick) or INTERVAL
            count += rate * step / 3600.0
            tick += INTERVAL
    return samples


@pytest.fixture
def drop_and_recovery():
    """40/min until FIRE_TICK, dead for 1800 ticks, then back to 40/min."""
    return make_samples([(FIRE_TICK, 40.0), (1800, 0.0), (7200, 40.0)])


@pytest.fixture
def never_recovers():
    """40/min until FIRE_TICK, then flatline forever."""
    return make_samples([(FIRE_TICK, 40.0), (9000, 0.0)])


@pytest.fixture
def slow_recovery():
    """40/min until FIRE_TICK, dead for 1800 ticks, a slow 10/min trickle for
    5400 ticks, then back to 40/min -- an "organic regrowth" curve that
    eventually crosses the recovery threshold, just much later than
    ``drop_and_recovery``'s step-function snap-back."""
    return make_samples(
        [(FIRE_TICK, 40.0), (1800, 0.0), (5400, 10.0), (9000, 40.0)]
    )


class TestThroughputSeries:
    def test_flat_rate_is_recovered(self):
        samples = make_samples([(7200, 40.0)])
        series = throughput_series(samples, "iron-plate")
        assert len(series) > 0
        for _, rate in series:
            assert rate == pytest.approx(40.0, rel=0.05)

    def test_early_samples_without_full_window_are_omitted(self):
        samples = make_samples([(7200, 40.0)])
        series = throughput_series(samples, "iron-plate", window_ticks=1800)
        assert series[0][0] >= 1800

    def test_empty_samples(self):
        assert throughput_series([], "iron-plate") == []

    def test_detects_the_drop(self, drop_and_recovery):
        series = dict(throughput_series(drop_and_recovery, "iron-plate"))
        # Trailing rate 1800+ ticks into the dead zone must be ~0
        dead_ticks = [t for t in series if FIRE_TICK + 1800 <= t <= FIRE_TICK + 1840]
        assert dead_ticks and series[dead_ticks[0]] == pytest.approx(0.0, abs=1.0)


class TestFrozenBaseline:
    def test_flat_baseline(self, drop_and_recovery):
        baseline = frozen_baseline(drop_and_recovery, "iron-plate", FIRE_TICK)
        assert baseline == pytest.approx(40.0, rel=0.05)

    def test_frozen_against_post_fire_behavior(
        self, drop_and_recovery, never_recovers
    ):
        # The baseline only looks backwards: identical pre-fire curves give
        # identical baselines regardless of what happens after.
        b1 = frozen_baseline(drop_and_recovery, "iron-plate", FIRE_TICK)
        b2 = frozen_baseline(never_recovers, "iron-plate", FIRE_TICK)
        assert b1 == pytest.approx(b2)

    def test_insufficient_samples_returns_none(self):
        assert frozen_baseline([], "iron-plate", FIRE_TICK) is None
        one = [{"tick": FIRE_TICK - 100, "counts": {"iron-plate": 5}}]
        assert frozen_baseline(one, "iron-plate", FIRE_TICK) is None


class TestThroughputRetained:
    def test_noop_never_recovers_is_near_zero(self, never_recovers):
        tr = throughput_retained(never_recovers, "iron-plate", FIRE_TICK, 3600)
        assert tr == pytest.approx(0.0, abs=0.05)

    def test_partial_recovery(self, drop_and_recovery):
        # 1800 dead ticks then full rate over a 3600-tick horizon -> ~0.5
        tr = throughput_retained(drop_and_recovery, "iron-plate", FIRE_TICK, 3600)
        assert tr == pytest.approx(0.5, abs=0.1)

    def test_full_retention(self):
        samples = make_samples([(FIRE_TICK + 7200, 40.0)])
        tr = throughput_retained(samples, "iron-plate", FIRE_TICK, 3600)
        assert tr == pytest.approx(1.0, abs=0.05)

    def test_winsorized_overshoot(self):
        # Rate quadruples post-fire: raw ratio 4.0 must be clamped to 1.5
        samples = make_samples([(FIRE_TICK, 40.0), (7200, 160.0)])
        tr = throughput_retained(samples, "iron-plate", FIRE_TICK, 3600)
        assert tr == 1.5

    def test_near_zero_baseline_returns_none(self):
        # Denominator edge case (documented in fle.disruptions.scoring): a
        # dead pre-fire factory yields no meaningful ratio -> None, not a
        # ZeroDivisionError and not a fake 0.0.
        samples = make_samples([(FIRE_TICK, 0.0), (3600, 40.0)])
        tr = throughput_retained(samples, "iron-plate", FIRE_TICK, 3600)
        assert tr is None

    def test_no_samples_returns_none(self):
        assert throughput_retained([], "iron-plate", FIRE_TICK, 3600) is None


def _fire_event(kind="entity_destruction", same_type_total=2, tick=FIRE_TICK, name="stone-furnace"):
    """A minimal ledger 'fired' entry carrying the redundancy count
    server.lua's KINDS.entity_destruction stashes on its manifest entry
    (fle/env/tools/admin/inject_disruption/server.lua). Mirrors the real
    shape LedgerEntry actually produces: ``affected`` is a top-level field
    (never routed through ``detail``) -- see
    fle.disruptions.scoring._redundancy_total's docstring. Pass
    ``same_type_total=None`` to build a fire event missing the field
    entirely (simulating older ledger data / a kind that never sets it)."""
    entry = {"name": name, "x": 0.0, "y": 0.0}
    if same_type_total is not None:
        entry["same_type_total"] = same_type_total
    return LedgerEntry(tick=tick, event="fired", kind=kind, seed=0, affected=[entry])


class TestFloorAdjustedThroughputRetained:
    """floor_adjusted_throughput_retained[_parts]: fixes the gap where a
    no-op agent with 2 identical furnaces (1 killed) scores plain TR ~= 0.5
    instead of ~= 0, by subtracting a floor anchored to the redundancy count
    frozen at fire time (server.lua's same_type_total, counted BEFORE
    e.die())."""

    def test_no_redundancy_reduces_to_plain_tr(self, drop_and_recovery):
        """Single point of failure (same_type_total=1): floor == 0 exactly,
        so the floor-adjusted parts/scalar must equal the plain TR output
        EXACTLY -- the reduction property the design calls out as
        load-bearing, not incidental."""
        fire = _fire_event(same_type_total=1)
        plain_parts = throughput_retained_parts(
            drop_and_recovery, "iron-plate", FIRE_TICK, 3600
        )
        adjusted_parts = floor_adjusted_throughput_retained_parts(
            drop_and_recovery, "iron-plate", FIRE_TICK, 3600, fire
        )
        assert adjusted_parts == plain_parts
        assert floor_adjusted_throughput_retained(
            drop_and_recovery, "iron-plate", FIRE_TICK, 3600, fire
        ) == throughput_retained(drop_and_recovery, "iron-plate", FIRE_TICK, 3600)

    def test_noop_with_redundancy_scores_near_zero(self):
        """Two furnaces, one destroyed, agent does nothing: real production
        settles at the surviving half (~0.5x baseline) -- exactly the
        passive-redundancy floor -- so floor-adjusted TR must be ~0, unlike
        plain TR, which stays stuck near 0.5 (the gap this feature fixes)."""
        samples = make_samples([(FIRE_TICK, 40.0), (3600, 20.0)])
        fire = _fire_event(same_type_total=2)
        tr_adj = floor_adjusted_throughput_retained(
            samples, "iron-plate", FIRE_TICK, 3600, fire
        )
        assert tr_adj == pytest.approx(0.0, abs=0.05)
        # Contrast: plain (unadjusted) TR is still ~0.5 here -- the gap
        # being fixed by this feature.
        assert throughput_retained(
            samples, "iron-plate", FIRE_TICK, 3600
        ) == pytest.approx(0.5, abs=0.05)

    def test_full_repair_scores_near_one(self):
        """Oracle repair back to the full baseline rate: floor-adjusted TR
        must be ~= 1 (the agent's own recovery covers the whole gap above
        the passive-redundancy floor)."""
        samples = make_samples([(FIRE_TICK + 7200, 40.0)])
        fire = _fire_event(same_type_total=2)
        tr_adj = floor_adjusted_throughput_retained(
            samples, "iron-plate", FIRE_TICK, 3600, fire
        )
        assert tr_adj == pytest.approx(1.0, abs=0.05)

    def test_non_entity_destruction_kind_is_none(self, drop_and_recovery):
        # Deliberate v1 scope limit: only entity_destruction has a cheap
        # redundancy count. belt_cut/resource_exhaustion/adaptive_strike
        # must return None even if same_type_total happens to be present.
        for kind in ("belt_cut", "resource_exhaustion", "adaptive_strike"):
            fire = _fire_event(kind=kind, same_type_total=2)
            assert (
                floor_adjusted_throughput_retained_parts(
                    drop_and_recovery, "iron-plate", FIRE_TICK, 3600, fire
                )
                is None
            )

    def test_missing_redundancy_field_is_none(self, drop_and_recovery):
        fire = _fire_event(same_type_total=None)
        assert (
            floor_adjusted_throughput_retained_parts(
                drop_and_recovery, "iron-plate", FIRE_TICK, 3600, fire
            )
            is None
        )

    def test_empty_affected_is_none(self, drop_and_recovery):
        fire = LedgerEntry(
            tick=FIRE_TICK, event="fired", kind="entity_destruction", seed=0, affected=[]
        )
        assert (
            floor_adjusted_throughput_retained_parts(
                drop_and_recovery, "iron-plate", FIRE_TICK, 3600, fire
            )
            is None
        )

    def test_degenerate_baseline_is_none(self):
        samples = make_samples([(FIRE_TICK, 0.0), (3600, 40.0)])
        fire = _fire_event(same_type_total=2)
        assert (
            floor_adjusted_throughput_retained_parts(
                samples, "iron-plate", FIRE_TICK, 3600, fire
            )
            is None
        )


class TestFloorAdjustedClosesSelfSabotageExploit:
    """The load-bearing test for this feature: proves the redundancy floor
    -- anchored to data fixed the instant the disruption fires (server.lua
    counts same_type_total BEFORE e.die()) -- cannot be manipulated by the
    agent's own post-fire behavior.

    An earlier design attempt inferred the floor from wherever post-fire
    production plateaus. It was adversarially rejected because an agent
    could self-sabotage (deconstruct/depower the survivor) right after the
    fire to manufacture an artificially low observed floor, then "recover"
    from its own self-inflicted damage to collect free credit. These tests
    prove the shipped design closes that exploit: the floor -- and hence the
    adjusted score -- depends only on the fire event's frozen redundancy
    count and the true endpoints of production, never on the shape of the
    post-fire curve in between.
    """

    def test_floor_is_invariant_to_post_fire_sample_shape(self):
        # Same pre-fire baseline (40/min) and same redundancy (2 furnaces,
        # one destroyed) in both cases. Both end at the IDENTICAL cumulative
        # production over the 3600-tick horizon (20 items -- half of the
        # 40-item expected baseline): the agent genuinely earned the exact
        # same amount of real recovered production in both cases.
        #
        # "clean": steady half-rate the whole horizon (no sabotage).
        horizon = 3600
        half = 1800
        clean = make_samples([(FIRE_TICK, 40.0), (horizon, 20.0)])
        # "sabotage": production craters to 0 for the first half of the
        # horizon (as if the agent deconstructed the survivor right after
        # the fire), then spikes to full rate for the second half, landing
        # at (approximately -- see the tolerance note below) the same final
        # cumulative total as "clean".
        sabotage = make_samples([(FIRE_TICK, 40.0), (half, 0.0), (half, 40.0)])
        fire = _fire_event(same_type_total=2)

        clean_parts = floor_adjusted_throughput_retained_parts(
            clean, "iron-plate", FIRE_TICK, horizon, fire
        )
        sabotage_parts = floor_adjusted_throughput_retained_parts(
            sabotage, "iron-plate", FIRE_TICK, horizon, fire
        )
        assert clean_parts is not None and sabotage_parts is not None
        # Both actual AND floor-adjusted-expected match, up to the sample
        # grid's own quantization noise (make_samples steps in fixed
        # 41-tick increments, so a piecewise-linear curve with an extra
        # segment boundary -- the sabotage dip -- lands within a fraction of
        # an item of the single-segment "clean" curve's true integral, not
        # bit-for-bit identical). That noise floor is <0.5 items on a
        # ~20-item actual and, in ratio terms, ~0.014 -- roughly 35x smaller
        # than the ~0.49 ratio gap the next test shows a plateau-based floor
        # would have handed the saboteur. The noise here is a sampling
        # artifact of the synthetic fixture, not the effect under test.
        assert clean_parts[0] == pytest.approx(sabotage_parts[0], abs=0.5)
        assert clean_parts[1] == pytest.approx(sabotage_parts[1], abs=0.5)

        clean_tr = floor_adjusted_throughput_retained(
            clean, "iron-plate", FIRE_TICK, horizon, fire
        )
        sabotage_tr = floor_adjusted_throughput_retained(
            sabotage, "iron-plate", FIRE_TICK, horizon, fire
        )
        assert clean_tr == pytest.approx(sabotage_tr, abs=0.02)
        # Neither ever exceeded the passive-redundancy floor, so both
        # correctly land at "no genuine recovery credit" -- 0 -- despite the
        # sabotage dip.
        assert clean_tr == pytest.approx(0.0, abs=0.05)

    def test_a_plateau_based_floor_would_have_rewarded_the_sabotage(self):
        """Illustrative contrast, not exercising production code: shows the
        REJECTED design (floor = wherever post-fire production plateaus,
        approximated here as its own observed minimum trailing rate,
        integrated over the horizon) WOULD have scored the self-sabotage
        trajectory materially higher than the clean one, despite both
        delivering identical real recovered production -- precisely the
        exploit this feature's actual design closes (see the test above,
        where the two trajectories score identically)."""
        horizon = 3600
        half = 1800
        clean = make_samples([(FIRE_TICK, 40.0), (horizon, 20.0)])
        sabotage = make_samples([(FIRE_TICK, 40.0), (half, 0.0), (half, 40.0)])

        def naive_plateau_adjusted_tr(samples):
            plain = throughput_retained_parts(
                samples, "iron-plate", FIRE_TICK, horizon
            )
            actual, expected = plain
            post = [s for s in samples if s["tick"] >= FIRE_TICK]
            series = throughput_series(post, "iron-plate", window_ticks=900)
            min_rate = min((r for _, r in series), default=0.0)
            naive_floor = min_rate * horizon / 3600.0
            denom = expected - naive_floor
            if denom <= 0:
                return None
            return (actual - naive_floor) / denom

        naive_clean = naive_plateau_adjusted_tr(clean)
        naive_sabotage = naive_plateau_adjusted_tr(sabotage)
        assert naive_clean is not None and naive_sabotage is not None
        # The rejected design rewards the sabotage trajectory strictly more,
        # even though real recovered production was (near-)identical: the
        # exploit. Measured: naive_clean ~= 0.01, naive_sabotage ~= 0.50 --
        # self-sabotage alone buys ~0.49 of free credit under that design.
        assert naive_sabotage > naive_clean + 0.1

        # ...while this feature's actual floor_adjusted_throughput_retained
        # gives them (up to the ~0.014 sampling noise quantified in the test
        # above) the identical, non-inflated score.
        fire = _fire_event(same_type_total=2)
        real_clean = floor_adjusted_throughput_retained(
            clean, "iron-plate", FIRE_TICK, horizon, fire
        )
        real_sabotage = floor_adjusted_throughput_retained(
            sabotage, "iron-plate", FIRE_TICK, horizon, fire
        )
        assert real_clean == pytest.approx(real_sabotage, abs=0.02)


class TestRecoveryAt:
    def test_recovers_within_budget(self, drop_and_recovery):
        # Dead 1800 ticks, then full rate. The trailing window (1800) needs
        # ~1800 more ticks at full rate before the rate crosses 0.9x, so a
        # 7200-tick budget comfortably contains recovery.
        assert (
            recovery_at(drop_and_recovery, "iron-plate", FIRE_TICK, 7200) is True
        )

    def test_not_within_a_tight_budget(self, drop_and_recovery):
        # A budget shorter than the trailing window has no valid post-fire
        # rate points (windows straddling the fire are excluded by design),
        # so recovery cannot be claimed.
        assert (
            recovery_at(drop_and_recovery, "iron-plate", FIRE_TICK, 1600) is False
        )
        # And with a budget that covers the dead zone but not the refill of
        # the trailing window, the rate has not yet crossed 0.9x baseline.
        assert (
            recovery_at(drop_and_recovery, "iron-plate", FIRE_TICK, 3000) is False
        )

    def test_never_recovers(self, never_recovers):
        assert (
            recovery_at(never_recovers, "iron-plate", FIRE_TICK, 9000) is False
        )

    def test_near_zero_baseline_returns_none(self):
        samples = make_samples([(FIRE_TICK, 0.0), (3600, 40.0)])
        assert recovery_at(samples, "iron-plate", FIRE_TICK, 3600) is None


class TestTimeToRecovery:
    """Continuous time-to-recovery: the efficiency axis recovery_at cannot
    express on its own, since two both-successful recoveries collapse to the
    same `True` regardless of how many ticks each one took."""

    def test_clean_fast_recovery(self, drop_and_recovery):
        parts = time_to_recovery_parts(
            drop_and_recovery, "iron-plate", FIRE_TICK, 7200
        )
        assert parts["recovered"] is True
        assert parts["budget_ticks"] == 7200
        # Dead for 1800 ticks, then the trailing window (1800) needs to fill
        # with full-rate production before crossing 0.9x -- recovery lands
        # sometime after the dead zone but comfortably inside the budget.
        assert 1800 < parts["ticks"] < 7200
        assert time_to_recovery(
            drop_and_recovery, "iron-plate", FIRE_TICK, 7200
        ) == pytest.approx(parts["ticks"])

    def test_slow_but_eventual_recovery_takes_longer_than_fast(
        self, drop_and_recovery, slow_recovery
    ):
        fast = time_to_recovery(drop_and_recovery, "iron-plate", FIRE_TICK, 16200)
        slow = time_to_recovery(slow_recovery, "iron-plate", FIRE_TICK, 16200)
        assert fast is not None and slow is not None
        # This is exactly the distinction TR (and the binary recovery_at)
        # cannot make once both curves cross the same threshold: a
        # step-function snap-back and a slow organic ramp both eventually
        # "recover", but the continuous metric orders them by speed.
        assert slow > fast

    def test_never_recovers_is_censored_at_budget(self, never_recovers):
        parts = time_to_recovery_parts(never_recovers, "iron-plate", FIRE_TICK, 9000)
        assert parts["recovered"] is False
        # Right-censored: we only know the true recovery time is >= budget,
        # so the censored duration IS the budget, not None and not a bare
        # guess -- this is what lets a downstream Kaplan-Meier estimator
        # treat it correctly instead of silently dropping the episode.
        assert parts["ticks"] == 9000.0
        assert time_to_recovery(never_recovers, "iron-plate", FIRE_TICK, 9000) == 9000.0

    def test_near_zero_baseline_returns_none(self):
        # Same denominator policy as throughput_retained/recovery_at: a
        # degenerate pre-fire baseline is "not scoreable", which must not be
        # confused with the real, meaningful recovered=False outcome above.
        samples = make_samples([(FIRE_TICK, 0.0), (3600, 40.0)])
        assert time_to_recovery_parts(samples, "iron-plate", FIRE_TICK, 3600) is None
        assert time_to_recovery(samples, "iron-plate", FIRE_TICK, 3600) is None

    def test_no_samples_returns_none(self):
        assert time_to_recovery_parts([], "iron-plate", FIRE_TICK, 3600) is None
        assert time_to_recovery([], "iron-plate", FIRE_TICK, 3600) is None

    def test_consistent_with_recovery_at(self, drop_and_recovery, never_recovers):
        # time_to_recovery_parts' `recovered` flag must agree with
        # recovery_at's boolean for the same inputs (parts is a strict
        # refinement, not a different notion of "recovered").
        assert (
            time_to_recovery_parts(drop_and_recovery, "iron-plate", FIRE_TICK, 7200)[
                "recovered"
            ]
            == recovery_at(drop_and_recovery, "iron-plate", FIRE_TICK, 7200)
        )
        assert (
            time_to_recovery_parts(never_recovers, "iron-plate", FIRE_TICK, 9000)[
                "recovered"
            ]
            == recovery_at(never_recovers, "iron-plate", FIRE_TICK, 9000)
        )


@pytest.fixture
def touch_and_abandon():
    """40/min until FIRE_TICK, dead 1800 ticks, a full-rate spike for 2400
    ticks (long enough to fully fill the 1800-tick trailing window), then
    dead again for good -- "touch the baseline once, then abandon it"."""
    return make_samples(
        [(FIRE_TICK, 40.0), (1800, 0.0), (2400, 40.0), (5400, 0.0)]
    )


class TestRecoveryPotential:
    """Phi(tick): the potential-based signal shaped_reward_delta is built
    from. See recovery_potential's docstring for the full design rationale;
    these tests are the adversarial proof of its gaming-resistance claims."""

    def test_phi_at_fire_tick_is_always_zero(self, drop_and_recovery, never_recovers):
        # No post-fire trailing window can be full at the instant of the
        # fire, regardless of what the curve does afterward -- this is what
        # lets orchestration reset "phi_prev" to 0 on a new fire without a
        # separately stored scalar (see fle/eval/inspect/wrench.py).
        for samples in (drop_and_recovery, never_recovers):
            assert recovery_potential(samples, "iron-plate", FIRE_TICK, FIRE_TICK) == 0.0

    def test_tick_before_fire_returns_none(self, drop_and_recovery):
        assert (
            recovery_potential(drop_and_recovery, "iron-plate", FIRE_TICK, FIRE_TICK - 100)
            is None
        )

    def test_overshoot_clips_to_one(self):
        # Rate quadruples post-fire (mirrors TestThroughputRetained's
        # winsorized-overshoot case) -- Phi must clip to 1.0, never read the
        # raw 4.0 ratio. This is the upper half of "Phi never exceeds 1 or
        # goes below 0 regardless of input".
        samples = make_samples([(FIRE_TICK, 40.0), (7200, 160.0)])
        phi = recovery_potential(samples, "iron-plate", FIRE_TICK, FIRE_TICK + 7200)
        assert phi == 1.0

    def test_counter_regression_floors_at_zero(self):
        # A production counter that goes backwards (e.g. a stats reset)
        # must not send Phi negative -- the clip floors at 0, same as a
        # dead factory, never a penalty channel below "producing nothing".
        # This is the lower half of the same bounds claim.
        samples = make_samples([(FIRE_TICK, 40.0), (3600, 0.0)])
        samples.append(
            {"tick": samples[-1]["tick"] + 41, "counts": {"iron-plate": -1000.0}}
        )
        phi = recovery_potential(samples, "iron-plate", FIRE_TICK, samples[-1]["tick"])
        assert phi is not None
        assert 0.0 <= phi <= 1.0

    def test_never_recovers_stays_at_floor(self, never_recovers):
        # A flatlined factory never exceeds baseline, so Phi should sit at
        # its floor (0) throughout the post-fire episode -- exercised at
        # every sample tick, not just the endpoints.
        for t, _ in throughput_series(never_recovers, "iron-plate"):
            if t < FIRE_TICK:
                continue
            phi = recovery_potential(never_recovers, "iron-plate", FIRE_TICK, t)
            assert phi is not None
            assert 0.0 <= phi <= 1.0

    def test_degenerate_baseline_returns_none(self):
        samples = make_samples([(FIRE_TICK, 0.0), (3600, 40.0)])  # dead pre-fire
        assert (
            recovery_potential(samples, "iron-plate", FIRE_TICK, FIRE_TICK + 3600)
            is None
        )

    def test_no_samples_returns_none(self):
        assert recovery_potential([], "iron-plate", FIRE_TICK, FIRE_TICK) is None


class TestShapedRewardDelta:
    """Adversarial tests for the gaming-resistance properties the design
    claims for the potential-based reward-shaping signal (Ng, Harada &
    Russell 1999): telescoping sums and no reward for abandoned recovery."""

    def test_telescoping_sum_invariant_to_step_granularity(self, drop_and_recovery):
        """The SAME recovery curve, measured as one coarse "big jump" delta
        spanning the whole post-fire episode vs. summed over many "tiny
        step" deltas at every ~41-tick sample in between, earns identical
        total shaped reward. This is the telescoping property that makes
        "creep the rate up in imperceptible increments forever, banking
        tiny rewards" structurally impossible: there is no way to earn more
        total reward by slicing the same trajectory more finely.
        """
        samples = drop_and_recovery
        item = "iron-plate"
        post_fire_ticks = sorted(
            s["tick"] for s in samples if s["tick"] >= FIRE_TICK
        )
        end_tick = post_fire_ticks[-1]

        # One big jump: a single delta spanning the whole span.
        coarse_total = shaped_reward_delta(samples, item, FIRE_TICK, FIRE_TICK, end_tick)
        assert coarse_total is not None

        # Many tiny steps: a delta for every consecutive pair of post-fire
        # sample ticks, summed.
        fine_total = 0.0
        prev_tick = FIRE_TICK
        for t in post_fire_ticks:
            d = shaped_reward_delta(samples, item, FIRE_TICK, prev_tick, t)
            assert d is not None
            fine_total += d
            prev_tick = t

        assert fine_total == pytest.approx(coarse_total, abs=1e-9)
        # And both agree with Phi read directly at the endpoints
        # (Phi(fire_tick) == 0, so the delta is just Phi(end_tick)).
        assert coarse_total == pytest.approx(
            recovery_potential(samples, item, FIRE_TICK, end_tick), abs=1e-9
        )

    def test_touch_baseline_once_then_abandon_does_not_keep_reward(
        self, touch_and_abandon
    ):
        """A policy that briefly touches the baseline and then abandons
        recovery must not walk away with the reward it earned during the
        touch -- a later negative delta has to cancel it. This is the
        structural fix for the "touch it once, keep the credit forever"
        exploit a running-max (ratchet) version of Phi would reopen."""
        samples = touch_and_abandon
        item = "iron-plate"
        peak_tick = FIRE_TICK + 1800 + 2400  # end of the full-rate spike
        final_tick = samples[-1]["tick"]

        phi_peak = recovery_potential(samples, item, FIRE_TICK, peak_tick)
        phi_final = recovery_potential(samples, item, FIRE_TICK, final_tick)
        assert phi_peak == pytest.approx(1.0, abs=0.1)  # briefly touched baseline
        assert phi_final == pytest.approx(0.0, abs=0.1)  # abandoned -- decayed back down

        # The delta covering the abandonment must be negative, and it must
        # exactly cancel the credit banked during the spike.
        late_delta = shaped_reward_delta(samples, item, FIRE_TICK, peak_tick, final_tick)
        assert late_delta is not None
        assert late_delta < 0
        assert phi_peak + late_delta == pytest.approx(phi_final, abs=1e-9)

        # Net total shaped reward across the whole episode is ~0 -- the
        # spike earned no permanent credit once the policy abandoned it.
        total = shaped_reward_delta(samples, item, FIRE_TICK, FIRE_TICK, final_tick)
        assert total == pytest.approx(0.0, abs=0.1)

    def test_degenerate_baseline_returns_none(self):
        samples = make_samples([(FIRE_TICK, 0.0), (3600, 40.0)])  # dead pre-fire
        assert (
            shaped_reward_delta(
                samples, "iron-plate", FIRE_TICK, FIRE_TICK, FIRE_TICK + 3600
            )
            is None
        )

    def test_one_degenerate_endpoint_propagates_none(self, drop_and_recovery):
        # prev_tick before fire_tick makes only ONE endpoint unscoreable --
        # the whole delta must still propagate None, not silently fall back
        # to treating the missing endpoint as 0.
        assert (
            shaped_reward_delta(
                drop_and_recovery,
                "iron-plate",
                FIRE_TICK,
                FIRE_TICK - 100,
                FIRE_TICK + 3600,
            )
            is None
        )

    def test_no_samples_returns_none(self):
        assert (
            shaped_reward_delta([], "iron-plate", FIRE_TICK, FIRE_TICK, FIRE_TICK)
            is None
        )


def fired(tick, affected):
    return LedgerEntry(tick=tick, event="fired", kind="entity_destruction",
                       seed=1, affected=affected)


def report(tick, x, y, cause="looks broken"):
    return LedgerEntry(tick=tick, event="report_fault",
                       detail={"x": x, "y": y, "cause": cause})


class TestDetectionMetrics:
    def test_clean_detection(self):
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        ledger = [fire, report(6500, 2.5, 0.5)]
        m = detection_metrics(ledger, [fire])
        assert m["latencies"] == [500]
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0

    def test_false_positive_report(self):
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        ledger = [
            fire,
            report(6500, 2.0, 0.0),        # true positive
            report(7000, 500.0, 500.0),    # nowhere near anything
        ]
        m = detection_metrics(ledger, [fire])
        assert m["precision"] == pytest.approx(0.5)
        assert m["recall"] == 1.0
        assert m["latencies"] == [500]

    def test_report_before_fire_does_not_count(self):
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        ledger = [report(5000, 2.0, 0.0), fire]
        m = detection_metrics(ledger, [fire])
        assert m["latencies"] == []
        assert m["recall"] == 0.0
        assert m["precision"] == 0.0

    def test_report_outside_radius_does_not_count(self):
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        ledger = [fire, report(6500, 30.0, 0.0)]
        m = detection_metrics(ledger, [fire], radius=10.0)
        assert m["recall"] == 0.0

    def test_first_matching_report_sets_latency(self):
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        ledger = [fire, report(8000, 2.0, 0.0), report(6100, 2.0, 0.0)]
        m = detection_metrics(ledger, [fire])
        assert m["latencies"] == [100]

    def test_no_reports_no_fires_vacuous(self):
        m = detection_metrics([], [])
        assert m == {
            "latencies": [],
            "precision": 1.0,
            "precision_strict": 1.0,
            "recall": 1.0,
        }

    def test_strict_radius_separates_nearby_from_exact(self):
        # the first Sonnet pilot: a full-chest report 6.7 tiles from the
        # destroyed drill counted as a match at radius 10 but should not
        # at the strict 3-tile radius
        fire = fired(6000, [{"name": "burner-mining-drill", "x": 16.0, "y": 71.0}])
        exact = report(7000, x=16.0, y=71.0)
        nearby = report(8000, x=20.5, y=75.5)  # ~6.7 tiles away
        m = detection_metrics([fire, exact, nearby], [fire])
        # Anti-spam diminishing-returns cap (see detection_counts): credit
        # for a fired event is capped at its earliest matching report, so
        # the second (nearby) report about the SAME single fault adds to
        # num_reports (volume) without adding further numerator credit.
        # precision == 0.5 sits exactly on DETECTION_PRECISION_FLOOR, which
        # is chosen so this legitimate double-report case is NOT gated
        # (0.5 is not < the floor) while an actual spam sweep is.
        assert m["precision"] == 0.5
        assert m["precision_strict"] == 0.5
        assert m["recall"] == 1.0  # not gated: precision sits at, not below, the floor

    def test_missed_fire(self):
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        m = detection_metrics([fire], [fire])
        assert m["recall"] == 0.0
        assert m["precision"] == 1.0  # vacuous: no reports, no false alarms

    def test_works_with_plain_dicts(self):
        fire = {
            "tick": 6000,
            "event": "fired",
            "affected": [{"name": "stone-furnace", "x": 2.0, "y": 0.0}],
        }
        rep = {
            "tick": 6300,
            "event": "report_fault",
            "detail": {"x": 1.0, "y": 1.0},
        }
        m = detection_metrics([fire, rep], [fire])
        assert m["latencies"] == [300]

    def test_report_spam_gets_no_extra_precision_credit(self):
        # Same accurate position, resubmitted repeatedly (the cheap,
        # cadence-based exploit this cap exists to close -- report_fault is
        # a free, unmetered tool: fle.eval.tasks.observability_budget
        # .DEFAULT_METERED_TOOLS does not include it). Before the cap this
        # scored identically to a single accurate report: precision=1.0,
        # recall=1.0.
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        spam = [report(6100 + 100 * i, 2.0, 0.0) for i in range(10)]
        ledger = [fire] + spam
        c = detection_counts(ledger, [fire])
        assert c["matched_reports"] == 1  # capped: one credit for the one fire
        assert c["num_reports"] == 10  # raw volume stays uncapped
        m = detection_metrics(ledger, [fire])
        assert m["precision"] == pytest.approx(0.1)
        assert m["recall"] == 0.0  # gated: precision (0.1) < DETECTION_PRECISION_FLOOR

    def test_single_accurate_report_is_not_gated(self):
        # The contrast case: one genuine, accurate report_fault call earns
        # full precision and is never touched by the gate.
        fire = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        ledger = [fire, report(6100, 2.0, 0.0)]
        m = detection_metrics(ledger, [fire])
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0

    def test_spam_across_distinct_fires_each_earns_one_credit(self):
        # The cap is per FIRED EVENT, not a global "first report only" --
        # an agent that accurately reports N genuinely distinct incidents
        # once each should not be penalized relative to one that reports
        # a single incident N times.
        fire1 = fired(6000, [{"name": "stone-furnace", "x": 2.0, "y": 0.0}])
        fire2 = fired(6500, [{"name": "stone-furnace", "x": 50.0, "y": 0.0}])
        ledger = [fire1, fire2, report(6100, 2.0, 0.0), report(6600, 50.0, 0.0)]
        c = detection_counts(ledger, [fire1, fire2])
        assert c["matched_reports"] == 2
        m = detection_metrics(ledger, [fire1, fire2])
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0
