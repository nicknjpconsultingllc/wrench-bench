"""Unit tests for fle.disruptions.episode (no server).

``episode_metrics`` is the single computation behind the Inspect scorers,
``WrenchEpisode.finalize`` and the verifiers rubric, so it is pinned here
against the underlying scoring functions on canned data. ``parse_code``
must accept exactly what the Inspect solver's ``parse_response`` accepts.
"""

import math

import pytest

from fle.disruptions.episode import (
    NO_CODE_FEEDBACK,
    WrenchEpisode,
    episode_metrics,
    parse_code,
    system_prompt_for,
)
from fle.disruptions.scoring import (
    detection_metrics,
    recovery_at,
    throughput_retained,
    winsorize_tr,
)
from fle.eval.tasks.task_definitions.disruption.sentinel_tasks import (
    IRON_PLATE_SENTINEL,
)

ITEM = "iron-plate"
STEP = 41  # engine sample interval


def _samples(rate_before, rate_after, fire_tick, end_tick):
    """Cumulative production at ~41-tick intervals with a rate change at
    fire_tick (rates in items/min)."""
    samples = []
    count = 0.0
    tick = 0
    while tick <= end_tick:
        rate = rate_before if tick < fire_tick else rate_after
        samples.append({"tick": tick, "counts": {ITEM: count}})
        count += rate * STEP / 3600
        tick += STEP
    return samples


def _fire(tick, x=2.0, y=0.0, seed=11, kind="entity_destruction"):
    return {
        "tick": tick,
        "event": "fired",
        "kind": kind,
        "seed": seed,
        # same_type_total rides on the manifest entry (see
        # scoring._redundancy_total), never in detail.
        "affected": [{"name": "stone-furnace", "x": x, "y": y, "same_type_total": 2}],
        "detail": {},
    }


def _report(tick, x, y):
    return {
        "tick": tick,
        "event": "report_fault",
        "kind": None,
        "seed": None,
        "affected": [],
        "detail": {"x": x, "y": y, "cause": "furnace gone"},
    }


class TestEpisodeMetrics:
    def test_unscoreable_when_nothing_fired(self):
        samples = _samples(40, 40, fire_tick=10**9, end_tick=20_000)
        m = episode_metrics(samples, [], ITEM, end_tick=20_000)
        assert m["num_fires"] == 0
        assert m["throughput_retained"]["value"] is None
        assert m["throughput_retained"]["metadata"]["scoreable"] is False
        assert m["recovery"]["value"] is None
        assert m["time_to_recovery"]["mean_ticks"] is None
        # Vacuous detection: nothing to find, nothing hallucinated.
        assert m["detection"]["value"] == 1.0
        assert m["detection"]["metadata"]["precision_strict"] == 1.0
        s = m["scalars"]
        assert s["throughput_retained"] is None
        assert s["tr_scoreable"] is False
        assert s["tr_pooled_denominator"] == 0.0
        assert s["detection_recall"] == 1.0

    def test_matches_scoring_functions_on_a_half_loss(self):
        fire_tick = 12_000
        end_tick = fire_tick + 6_000
        samples = _samples(40, 20, fire_tick, end_tick)
        fire = _fire(fire_tick)
        ledger = [{"tick": 1, "event": "armed", "kind": "entity_destruction"}, fire]

        m = episode_metrics(samples, ledger, ITEM, end_tick)
        horizon = end_tick - fire_tick
        expected_tr = throughput_retained(samples, ITEM, fire_tick, horizon)
        assert expected_tr is not None
        assert m["throughput_retained"]["value"] == pytest.approx(expected_tr)
        assert m["throughput_retained"]["value"] == pytest.approx(0.5, abs=0.05)
        meta = m["throughput_retained"]["metadata"]
        assert meta["scoreable"] is True
        assert meta["num_fires"] == 1
        assert meta["fires"][0]["horizon_ticks"] == horizon
        assert winsorize_tr(meta["pooled_numerator"] / meta["pooled_denominator"]) == (
            pytest.approx(expected_tr)
        )
        # Floor-adjusted pool is defined: entity_destruction with same_type_total.
        assert meta["floor_adjusted_scoreable"] is True
        assert meta["floor_adjusted_num_fires"] == 1

        assert m["recovery"]["value"] == float(
            recovery_at(samples, ITEM, fire_tick, horizon)
        )
        assert m["recovery"]["value"] == 0.0
        ttr = m["time_to_recovery"]
        assert ttr["fires"][0]["parts"]["recovered"] is False
        assert ttr["mean_ticks"] == float(horizon)  # right-censored at budget

        s = m["scalars"]
        assert s["throughput_retained_raw"] == pytest.approx(
            meta["pooled_numerator"] / meta["pooled_denominator"]
        )
        assert s["recovery_rate"] == 0.0
        assert s["num_fires"] == 1

    def test_recovery_and_detection(self):
        fire_tick = 12_000
        recover_tick = fire_tick + 1_500
        end_tick = fire_tick + 6_000
        # Full rate before, none for 1500 ticks, then full rate again.
        samples = []
        count = 0.0
        tick = 0
        while tick <= end_tick:
            rate = 0 if fire_tick <= tick < recover_tick else 40
            samples.append({"tick": tick, "counts": {ITEM: count}})
            count += rate * STEP / 3600
            tick += STEP
        fire = _fire(fire_tick, x=2.0, y=0.0)
        ledger = [
            fire,
            _report(fire_tick + 300, 2.5, 0.0),
            _report(fire_tick + 400, 9.0, 0.0),
        ]

        m = episode_metrics(samples, ledger, ITEM, end_tick)
        assert m["recovery"]["value"] == 1.0
        ttr = m["time_to_recovery"]["fires"][0]["parts"]
        assert ttr["recovered"] is True
        assert 0 < ttr["ticks"] < 6_000
        assert m["scalars"]["time_to_recovery_ticks"] == ttr["ticks"]

        det = detection_metrics(ledger, [fire])
        assert m["detection"]["metadata"]["recall"] == det["recall"] == 1.0
        assert m["detection"]["metadata"]["precision"] == det["precision"]
        # Second report is 7 tiles off: inside the loose radius, outside strict.
        assert m["detection"]["metadata"]["precision_strict"] == 0.5
        assert m["detection"]["metadata"]["mean_latency_ticks"] == 300.0
        assert m["scalars"]["detection_latency_ticks"] == 300.0

    def test_tracked_item_comes_from_samples(self):
        samples = [{"tick": 0, "counts": {"copper-cable": 0}}]
        m = episode_metrics(samples, [], "iron-plate", 0)
        assert m["item"] == "copper-cable"
        assert episode_metrics([], [], "iron-plate", 0)["item"] == "iron-plate"


class TestParseCode:
    def test_single_fenced_block(self):
        assert parse_code("Plan.\n```python\nprint(1)\n```\nDone.") == "print(1)"

    def test_multiple_blocks_are_combined(self):
        text = "```python\na = 1\n```\ntext\n```python\nprint(a)\n```"
        assert parse_code(text) == "a = 1\n\nprint(a)"

    def test_bare_python_is_taken_whole(self):
        assert parse_code("x = 1\nprint(x)") == "x = 1\nprint(x)"

    def test_bare_prose_becomes_a_comment_program(self):
        # FLE's parser salvage (inherited, and what the Inspect solver runs):
        # prose is wrapped as a comment and executed as a no-op program.
        assert parse_code("Just prose, no code here!") == "# Just prose, no code here!"

    @pytest.mark.parametrize("text", [None, "", "   ", "```python\n```"])
    def test_no_code(self, text):
        assert parse_code(text) is None


class TestOfflineEpisode:
    def test_construction_is_offline_and_seeded(self, monkeypatch, tmp_path):
        monkeypatch.delenv("WRENCH_LEDGER_DIR", raising=False)
        monkeypatch.delenv("WRENCH_TRAJECTORY_DIR", raising=False)
        ep = WrenchEpisode(
            IRON_PLATE_SENTINEL, seed_offset=2, run_idx=0, trajectory_length=5
        )
        assert ep.trajectory_length == 5
        assert ep.task_key == IRON_PLATE_SENTINEL
        assert ep.task.disruptions[0].seed == 11 + 2
        assert not ep.started
        assert not ep.is_done
        assert ep.writer is None
        assert ep.episode_name.startswith("iron_plate_sentinel_seed2_")
        with pytest.raises(RuntimeError):
            ep.observe()
        with pytest.raises(RuntimeError):
            ep.step("print(1)")
        # cleanup before start is a no-op, never raises
        ep.cleanup()

    def test_env_var_opt_ins_name_files_per_episode(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WRENCH_LEDGER_DIR", str(tmp_path / "ledgers"))
        monkeypatch.setenv("WRENCH_TRAJECTORY_DIR", str(tmp_path / "traj"))
        a = WrenchEpisode(IRON_PLATE_SENTINEL, run_idx=0)
        b = WrenchEpisode(IRON_PLATE_SENTINEL, run_idx=0)
        assert a.task.ledger_dir != b.task.ledger_dir
        assert a.task.ledger_dir.startswith(str(tmp_path / "ledgers"))
        assert a.writer.path != b.writer.path
        assert a.writer.path.parent == tmp_path / "traj"

    def test_system_prompt_states_the_contract_and_leaks_nothing(self):
        ep = WrenchEpisode(IRON_PLATE_SENTINEL, run_idx=0, trajectory_length=7)
        prompt = ep.system_prompt()
        assert prompt == system_prompt_for(ep.task, 7)
        assert "## TASK OBJECTIVE" in prompt
        assert ep.task.goal_description in prompt
        assert "You have 7 trajectory steps" in prompt
        assert "ONE ```python code block" in prompt
        # The API reference legitimately mentions "seed" (map-gen enums); the
        # task section must not name kinds, seeds, or the ledger.
        objective = prompt.split("## TASK OBJECTIVE", 1)[1].lower()
        for forbidden in ("seed", "ledger", "fire", "entity_destruction", "belt_cut"):
            assert forbidden not in objective

    def test_finalize_without_server_is_unscoreable(self):
        ep = WrenchEpisode(IRON_PLATE_SENTINEL, run_idx=0, trajectory_length=3)
        result = ep.finalize()
        assert result["num_fires"] == 0
        assert result["metrics"]["throughput_retained"] is None
        assert result["metrics"]["detection_recall"] == 1.0
        assert result["quota_met"] is False
        assert result is ep.finalize()  # idempotent
        assert NO_CODE_FEEDBACK.startswith("Your reply contained no")
        assert not math.isnan(result["quota"])
