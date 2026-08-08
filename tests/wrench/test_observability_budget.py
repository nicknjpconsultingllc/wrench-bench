"""Observability-budget mechanism: counter unit tests plus a live check that
the hook actually fires on agent-issued tool calls and the budget line
appears in the per-step observation.

See fle/eval/tasks/observability_budget.py for the soft-cap design
rationale (calls beyond budget still execute -- they are counted, not
blocked -- because the underlying tool-call hook API swallows exceptions
raised by callbacks, so a hard cap is not cleanly implementable through it).
"""

import pytest

from fle.disruptions import DisruptionKind, DisruptionSpec
from fle.disruptions.scoring import observability_budget_metrics
from fle.env.entities import Position
from fle.env.game_types import Prototype
from fle.eval.tasks import DisruptionRecoveryTask
from fle.eval.tasks.observability_budget import (
    DEFAULT_METERED_TOOLS,
    ObservabilityBudget,
)


def make_budgeted_task(tmp_path, budget=5, quota=30, holdout=15):
    return DisruptionRecoveryTask(
        trajectory_length=4,
        goal_description="WRENCH observability smoke test",
        task_key="wrench_observability_smoke",
        throughput_entity="iron-plate",
        quota=quota,
        holdout_wait_period=holdout,
        verify_windows=2,
        ledger_dir=str(tmp_path),
        observability_budget=budget,
        disruptions=[
            DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=11),
        ],
    )


class TestObservabilityBudgetUnit:
    """No server -- exercises the counter class as a plain Python object by
    driving it through a fake instance with the same hook-registration
    surface as fle.env.lua_manager.LuaScriptManager."""

    def test_default_metered_tools(self):
        assert DEFAULT_METERED_TOOLS == (
            "get_entity",
            "get_entities",
            "inspect_inventory",
        )
        # get_alerts is deliberately never metered; get_production_stats is
        # admin-only and not agent-callable, so it must never appear either.
        assert "get_alerts" not in DEFAULT_METERED_TOOLS
        assert "get_production_stats" not in DEFAULT_METERED_TOOLS

    def test_install_registers_a_pre_hook_per_metered_tool(self):
        class FakeInstance:
            pass

        inst = FakeInstance()
        budget = ObservabilityBudget(budget=3, metered_tools=("get_entity", "get_entities"))
        budget.install(inst)
        assert set(inst.pre_tool_hooks.keys()) == {"get_entity", "get_entities"}
        assert len(inst.pre_tool_hooks["get_entity"]) == 1

    def test_counts_calls_and_computes_remaining(self):
        class FakeInstance:
            pass

        inst = FakeInstance()
        budget = ObservabilityBudget(budget=2, metered_tools=("get_entity",))
        budget.install(inst)

        hook = inst.pre_tool_hooks["get_entity"][0]
        assert budget.remaining == 2
        hook(None)  # first call
        assert budget.used == 1
        assert budget.remaining == 1
        assert budget.over_budget == 0
        hook(None)  # second call: exactly at budget
        assert budget.remaining == 0
        assert budget.over_budget == 0
        hook(None)  # third call: over budget, but the hook itself never raises
        assert budget.used == 3
        assert budget.remaining == 0
        assert budget.over_budget == 1
        assert budget.calls_by_tool == {"get_entity": 3}

    def test_status_line_under_and_over_budget(self):
        class FakeInstance:
            pass

        inst = FakeInstance()
        budget = ObservabilityBudget(budget=1, metered_tools=("get_entity",))
        budget.install(inst)
        hook = inst.pre_tool_hooks["get_entity"][0]

        assert budget.status_line() == "Inspection calls remaining: 1/1"
        hook(None)
        assert budget.status_line() == "Inspection calls remaining: 0/1"
        hook(None)
        line = budget.status_line()
        assert "over budget by 1" in line
        assert "still work" in line

    def test_summary_shape(self):
        class FakeInstance:
            pass

        inst = FakeInstance()
        budget = ObservabilityBudget(budget=1, metered_tools=("get_entity", "get_entities"))
        budget.install(inst)
        inst.pre_tool_hooks["get_entity"][0](None)
        inst.pre_tool_hooks["get_entities"][0](None)
        inst.pre_tool_hooks["get_entities"][0](None)

        summary = budget.summary()
        assert summary["inspection_calls_used"] == 3
        assert summary["inspection_calls_budget"] == 1
        assert summary["inspection_calls_over_budget"] == 2
        assert summary["inspection_calls_within_budget"] is False
        assert summary["inspection_calls_by_tool"] == {
            "get_entity": 1,
            "get_entities": 2,
        }

    def test_install_is_idempotent_across_repeated_setup_instance(self):
        """A resumed episode may call setup_instance twice on the same live
        FactorioInstance; a second ObservabilityBudget's install() must not
        leave the first one's hook still counting alongside it."""

        class FakeInstance:
            pass

        inst = FakeInstance()
        first = ObservabilityBudget(budget=5, metered_tools=("get_entity",))
        first.install(inst)
        second = ObservabilityBudget(budget=5, metered_tools=("get_entity",))
        second.install(inst)

        assert len(inst.pre_tool_hooks["get_entity"]) == 1
        inst.pre_tool_hooks["get_entity"][0](None)
        assert first.used == 0  # stale counter untouched
        assert second.used == 1


class TestDisruptionTaskIntegrationUnit:
    """No server -- exercises DisruptionRecoveryTask's budget plumbing
    directly against a fake instance/engine, mirroring how setup_instance
    uses instance.controllers["inject_disruption"]."""

    class FakeEngine:
        def reset_state(self):
            pass

        def arm(self, **kwargs):
            return 1

    class FakeInstance:
        def __init__(self):
            self.controllers = {"inject_disruption": TestDisruptionTaskIntegrationUnit.FakeEngine()}

    def test_unbudgeted_task_has_no_budget_and_no_status_line(self, tmp_path):
        from tests.wrench.test_disruption_task import make_task

        task = make_task(tmp_path)
        task.setup_instance(self.FakeInstance())
        assert task.budget is None
        enhanced = task.enhance_response_with_task_output("output", _fake_response())
        assert "Inspection calls remaining" not in enhanced

    def test_budgeted_task_installs_budget_and_appends_status_line(self, tmp_path):
        task = make_budgeted_task(tmp_path, budget=7)
        inst = self.FakeInstance()
        task.setup_instance(inst)
        assert task.budget is not None
        assert task.budget.budget == 7
        assert set(inst.pre_tool_hooks.keys()) == set(DEFAULT_METERED_TOOLS)

        enhanced = task.enhance_response_with_task_output("output", _fake_response())
        assert "Inspection calls remaining: 7/7" in enhanced

    def test_budget_resets_across_setup_instance_calls(self, tmp_path):
        task = make_budgeted_task(tmp_path, budget=7)
        inst = self.FakeInstance()
        task.setup_instance(inst)
        inst.pre_tool_hooks["get_entity"][0](None)
        inst.pre_tool_hooks["get_entity"][0](None)
        assert task.budget.used == 2

        task.setup_instance(inst)  # simulate re-provisioning (resumed episode)
        assert task.budget.used == 0
        assert len(inst.pre_tool_hooks["get_entity"]) == 1


def _fake_response():
    from fle.agents import TaskResponse

    return TaskResponse(success=False, meta={})


class TestScoringMetrics:
    def test_no_budget_keys_returns_none(self):
        assert observability_budget_metrics({}) is None
        assert observability_budget_metrics({"wrench_events_drained": 2}) is None

    def test_within_budget(self):
        m = observability_budget_metrics(
            {"inspection_calls_used": 3, "inspection_calls_budget": 10}
        )
        assert m["calls_used"] == 3
        assert m["calls_budget"] == 10
        assert m["calls_over_budget"] == 0
        assert m["within_budget"] is True
        assert m["utilization"] == pytest.approx(0.3)

    def test_over_budget(self):
        m = observability_budget_metrics(
            {"inspection_calls_used": 12, "inspection_calls_budget": 10}
        )
        assert m["calls_over_budget"] == 2
        assert m["within_budget"] is False

    def test_zero_budget_never_divides_by_zero(self):
        m = observability_budget_metrics(
            {"inspection_calls_used": 0, "inspection_calls_budget": 0}
        )
        assert m["utilization"] is None
        assert m["within_budget"] is True


@pytest.mark.wrench_live
def test_live_hook_counts_agent_calls_and_status_line_appears(instance, tmp_path):
    """End-to-end: install a tight budget, run agent-style get_entity /
    get_entities calls through the namespace (exactly how DisruptionRecoveryTask's
    hooks see them during an episode), and confirm the counter and the
    per-step observation text both reflect real usage -- including staying
    callable (soft cap) once over budget."""
    budget_n = 10
    task = make_budgeted_task(tmp_path, budget=budget_n, quota=1000, holdout=1)
    task.setup(instance)
    instance.set_speed(10.0)
    assert task.budget is not None
    # TaskABC.setup() calls GameState.from_instance() right after
    # setup_instance(), which calls namespace.inspect_inventory() once per
    # agent as framework bookkeeping -- through the same hooked namespace
    # attribute an agent's own inspect_inventory() call would use, so it is
    # indistinguishable from the outside and gets counted. This is a fixed,
    # tiny, deterministic offset (num_agents calls) present in every
    # episode of any task using this mechanism; assert against a baseline
    # captured right after setup rather than an absolute 0 (see
    # fle/eval/tasks/observability_budget.py for more).
    baseline = task.budget.used
    assert baseline <= 1

    game = instance.namespace
    game.move_to(Position(x=0, y=0))

    # Three calls, still comfortably within budget.
    for _ in range(3):
        game.get_entities({Prototype.StoneFurnace})
    assert task.budget.used == baseline + 3
    assert task.budget.remaining == budget_n - (baseline + 3)

    response = task.verify(0.0, instance, {})
    enhanced = task.enhance_response_with_task_output("output", response)
    assert f"Inspection calls remaining: {budget_n - baseline - 3}/{budget_n}" in enhanced
    assert response.meta["inspection_calls_used"] == baseline + 3
    assert response.meta["inspection_calls_within_budget"] is True

    # Drain the rest of the budget exactly, then go one over.
    remaining = task.budget.remaining
    for _ in range(remaining):
        game.get_entities({Prototype.StoneFurnace})
    assert task.budget.remaining == 0
    assert task.budget.over_budget == 0

    # One more call is over budget but must still execute (soft cap): no
    # exception, still returns a normal result.
    result = game.get_entities({Prototype.StoneFurnace})
    assert isinstance(result, list)
    assert task.budget.over_budget == 1

    response2 = task.verify(0.0, instance, {})
    assert response2.meta["inspection_calls_within_budget"] is False
    enhanced2 = task.enhance_response_with_task_output("output", response2)
    assert "over budget by 1" in enhanced2

    # Unmetered get_alerts must not move the counter at all.
    before = task.budget.used
    game.get_alerts(5)
    assert task.budget.used == before
