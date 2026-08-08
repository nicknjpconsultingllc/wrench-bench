"""Unit tests for the WRENCH post-hoc scorers. Pure Python; no server."""

import pytest

from fle.disruptions.ledger import LedgerEntry
from fle.disruptions.scoring import (
    detection_metrics,
    frozen_baseline,
    recovery_at,
    throughput_retained,
    throughput_series,
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
        assert m == {"latencies": [], "precision": 1.0, "recall": 1.0}

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
