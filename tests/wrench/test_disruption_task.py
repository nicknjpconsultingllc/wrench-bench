"""DisruptionRecoveryTask verification.

Unit checks (no server) for config plumbing plus a live end-to-end check:
setup arms the engine, a scripted factory trips the precondition, the
disruption fires mid-episode, verify() measures a fixed-window mean and
drains every engine event into the ledger.
"""

import pytest

from fle.disruptions import DisruptionKind, DisruptionSpec
from fle.env.entities import Position
from fle.env.game_types import Prototype
from fle.eval.tasks import DisruptionRecoveryTask


def make_task(tmp_path, quota=30, holdout=15):
    return DisruptionRecoveryTask(
        trajectory_length=4,
        goal_description="WRENCH smoke test",
        task_key="wrench_smoke",
        throughput_entity="iron-plate",
        quota=quota,
        holdout_wait_period=holdout,
        verify_windows=2,
        ledger_dir=str(tmp_path),
        disruptions=[
            DisruptionSpec(kind=DisruptionKind.ENTITY_DESTRUCTION, seed=11),
        ],
    )


class TestUnit:
    def test_quota_item_from_string_and_prototype(self, tmp_path):
        assert make_task(tmp_path).quota_item == "iron-plate"
        task = DisruptionRecoveryTask(
            trajectory_length=4,
            goal_description="g",
            task_key="k",
            throughput_entity=Prototype.IronGearWheel,
            quota=16,
            holdout_wait_period=60,
        )
        assert task.quota_item == "iron-gear-wheel"

    def test_registry_round_trip_never_leaks_disruption_info(self):
        from fle.eval.tasks.task_definitions.task_registry import create_task

        for key in ("iron_plate_sentinel", "iron_gear_sentinel",
                    "copper_cable_sentinel", "iron_plate_observability_sentinel"):
            task = create_task(key)
            assert isinstance(task, DisruptionRecoveryTask)
            assert len(task.disruptions) == 2
            # The goal may mention that disruptions exist and the tools to
            # respond, but never schedule or seeds.
            goal = task.goal_description.lower()
            assert "report_fault" in goal
            assert "seed" not in goal
            assert "delay" not in goal
            for spec in task.disruptions:
                assert str(spec.seed) not in goal.replace("16", "")

    def test_observability_sentinel_carries_a_budget_the_others_do_not(self):
        from fle.eval.tasks.task_definitions.task_registry import create_task

        budgeted = create_task("iron_plate_observability_sentinel")
        assert budgeted.observability_budget_n is not None
        assert budgeted.observability_budget_n > 0
        # The budget mechanism (how many calls, which tools) is fair game to
        # disclose -- unlike disruption schedules/seeds -- since the agent
        # must know the rule to play against it.
        assert "inspection" in budgeted.goal_description.lower()
        assert "metered" in budgeted.goal_description.lower()

        for key in ("iron_plate_sentinel", "iron_gear_sentinel", "copper_cable_sentinel"):
            unbudgeted = create_task(key)
            assert unbudgeted.observability_budget_n is None


@pytest.mark.wrench_live
def test_end_to_end_verify_and_ledger(instance, tmp_path):
    task = make_task(tmp_path)
    task.setup(instance)
    instance.set_speed(10.0)
    assert task.engine_ids, "setup_instance must arm the disruption"

    # Scripted agent: the standard two-hand-fed-furnace iron-plate factory.
    game = instance.namespace
    game.move_to(Position(x=0, y=0))
    # LAB_PLAY starting inventory has no iron-ore; hand the fixture agent
    # its materials without disturbing the armed engine state.
    instance.first_namespace._set_inventory(
        {"stone-furnace": 4, "coal": 800, "iron-ore": 800}
    )
    for x in (2, 6):
        furnace = game.place_entity(
            Prototype.StoneFurnace, position=Position(x=x, y=0)
        )
        furnace = game.insert_item(Prototype.Coal, furnace, quantity=150)
        game.insert_item(Prototype.IronOre, furnace, quantity=190)

    # Wait (game time) for precondition -> fire, WITHOUT draining events:
    # verify() must be the one to ledger them. Observe the world instead.
    for _ in range(48):
        game.sleep(5)
        if len(game.get_entities({Prototype.StoneFurnace})) == 1:
            break
    else:
        raise AssertionError("disruption did not fire within 240 game-seconds")

    response = task.verify(0.0, instance, {})

    # Fixed-window mean, not max: post-disruption single-furnace output
    # (~18.75/min) cannot satisfy the 30/min quota.
    mean_throughput = response.meta[task.throughput_key]
    assert response.success is False
    assert 0 < mean_throughput < task.quota
    # armed + fired at minimum
    assert response.meta["wrench_events_drained"] >= 2

    entries = task.ledger.read()
    events = [e.event for e in entries]
    assert "armed" in events and "fired" in events
    fired = [e for e in entries if e.event == "fired"][0]
    assert fired.kind == "entity_destruction"
    assert fired.seed == 11
    assert len(fired.affected) == 1
    assert fired.affected[0]["name"] == "stone-furnace"
    assert (tmp_path / "wrench_smoke.jsonl").exists()

    # The agent-visible response carries the throughput report only.
    enhanced = task.enhance_response_with_task_output("output", response)
    assert "throughput" in enhanced
    lowered = enhanced.lower()
    for forbidden in ("disrupt", "seed", "fired", "armed", "ledger"):
        assert forbidden not in lowered
