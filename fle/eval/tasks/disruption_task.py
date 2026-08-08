"""WRENCH disruption-recovery task.

A DisruptionRecoveryTask is a ThroughputTask whose episode is perturbed by
seeded, precondition-gated disruptions (armed via the server-side WRENCH
engine). The verifier replaces the base class's unbounded max-over-windows
estimator with a fixed number of measurement windows averaged together, so a
factory that is degraded at verification time cannot "wait out" the estimator.

The agent is never told what was armed, when, or with what seed; every engine
event is drained into the append-only ledger for post-hoc scoring instead.
"""

import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from fle.agents import TaskResponse
from fle.commons.constants import REWARD_OVERRIDE_KEY
from fle.disruptions import DisruptionSpec, EventLedger, LedgerEntry
from fle.env import FactorioInstance
from fle.env.utils.achievements import eval_program_with_achievements
from fle.eval.tasks.throughput_task import ThroughputTask

# Keys the engine emits as first-class LedgerEntry fields; everything else
# lands in `detail` (e.g. report_fault's x/y/cause, failed's error).
_LEDGER_TOP_LEVEL_KEYS = {"tick", "event", "kind", "seed", "affected"}


class DisruptionRecoveryTask(ThroughputTask):
    def __init__(
        self,
        trajectory_length,
        goal_description: str,
        task_key: str,
        throughput_entity,
        quota: int,
        holdout_wait_period: int,
        pre_holdout_wait_period: int = 0,
        agent_instructions: Optional[List[str]] = None,
        disruptions: Optional[List[DisruptionSpec]] = None,
        ledger_dir: Optional[str] = None,
        verify_windows: int = 2,
    ):
        super().__init__(
            trajectory_length,
            goal_description,
            task_key,
            throughput_entity,
            quota,
            holdout_wait_period,
            pre_holdout_wait_period=pre_holdout_wait_period,
            agent_instructions=agent_instructions,
        )
        self.disruptions: List[DisruptionSpec] = list(disruptions or [])
        self.ledger_dir = ledger_dir
        self.verify_windows = verify_windows
        # spec index -> engine-side disruption id, filled in setup_instance
        self.engine_ids: Dict[int, int] = {}
        self.ledger: Optional[EventLedger] = None

    @property
    def quota_item(self) -> str:
        """The tracked item name (throughput_entity as a plain string)."""
        entity = self.throughput_entity
        value = getattr(entity, "value", entity)
        if isinstance(value, tuple):
            value = value[0]
        return str(value)

    def setup_instance(self, instance: FactorioInstance) -> None:
        engine = instance.controllers["inject_disruption"]
        engine.reset_state()
        self.engine_ids = {}
        for idx, spec in enumerate(self.disruptions):
            engine_id = engine.arm(
                kind=spec.kind.value,
                seed=spec.seed,
                quota_item=self.quota_item,
                quota_per_min=self.quota,  # quota is per 60s == per minute
                quota_fraction=spec.precondition.quota_fraction,
                consecutive_windows=spec.precondition.consecutive_windows,
                delay_ticks=spec.delay_ticks,
                params=dict(spec.params),
            )
            self.engine_ids[idx] = engine_id
        if self.ledger_dir is not None:
            ledger_path = Path(self.ledger_dir) / f"{self.task_key}.jsonl"
        else:
            ledger_path = (
                Path(tempfile.mkdtemp(prefix="wrench_ledger_"))
                / f"{self.task_key}.jsonl"
            )
        self.ledger = EventLedger(ledger_path)

    def verify(
        self, score: float, instance: FactorioInstance, step_statistics: Dict
    ) -> TaskResponse:
        # Fixed-window mean estimator: exactly verify_windows holdout windows,
        # averaged. No unbounded "keep going while it improves" loop -- a
        # disrupted factory must actually be recovered at verification time.
        readings = []
        for _ in range(self.verify_windows):
            _, _, _, achievements = eval_program_with_achievements(
                program=f"sleep({self.holdout_wait_period})", instance=instance
            )
            readings.append(achievements["dynamic"].get(self.throughput_entity, 0))
        mean_throughput = sum(readings) / len(readings) if readings else 0.0

        drained = self._drain_events_to_ledger(instance)

        return TaskResponse(
            success=mean_throughput >= self.quota,
            meta={
                self.throughput_key: mean_throughput,
                REWARD_OVERRIDE_KEY: mean_throughput,
                "wrench_events_drained": drained,
            },
        )

    def _drain_events_to_ledger(self, instance: FactorioInstance) -> int:
        """Drain engine events into the ledger; returns the number drained."""
        engine = instance.controllers["inject_disruption"]
        events = engine.drain_events()
        if self.ledger is None:
            # verify() without setup_instance() (e.g. resumed episode):
            # still ledger the events rather than dropping them.
            self.ledger = EventLedger(
                Path(tempfile.mkdtemp(prefix="wrench_ledger_"))
                / f"{self.task_key}.jsonl"
            )
        for event in events:
            self.ledger.append(
                LedgerEntry(
                    tick=event["tick"],
                    event=event["event"],
                    kind=event.get("kind"),
                    seed=event.get("seed"),
                    affected=event.get("affected", []),
                    detail={
                        k: v
                        for k, v in event.items()
                        if k not in _LEDGER_TOP_LEVEL_KEYS
                    },
                )
            )
        return len(events)

    # enhance_response_with_task_output is deliberately inherited unchanged:
    # the agent sees only the throughput report, never disruption info.
