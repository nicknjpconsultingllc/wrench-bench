"""One WRENCH disruption-recovery episode, harness-agnostic.

``WrenchEpisode`` owns everything between "a Factorio server slot was
allocated" and "here are the episode's numbers": connecting the
``FactorioInstance``, arming the task's seeded disruptions, running one
agent program per step through the gym environment, draining the engine's
production samples and ledger events incrementally, keeping the
potential-based reward-shaping account, and computing the post-hoc metrics
(``episode_metrics``) at the end.

Both drivers share it:

- ``fle.eval.inspect.wrench.wrench_solver`` (the Inspect path used for the
  published table) wraps it in Inspect's message/generate loop and copies
  its fields into the ``WrenchData`` store for the scorers.
- ``environments/wrench_factorio`` (the Prime Intellect ``verifiers``
  package) wraps it in a ``MultiTurnEnv``.

Neither driver talks to the engine, the ledger, or the gym environment
directly. The turn protocol is deliberately split into two calls so a
driver can decide what to do when the game-state read fails:

    obs = episode.observe()      # "{last feedback} --- Step k/N + game state"
    ...model turn...
    episode.step(code)           # run the program, verify, drain -> feedback

``step(None)`` is the "reply had no code block" turn (it still consumes a
step, exactly as the Inspect solver always did), ``skip_step`` /
``fail_step`` are the two model-side failure paths, and ``is_done`` flips
once ``trajectory_length`` turns are consumed. The episode never ends
early on quota success: disruptions arm on demonstrated throughput, so a
quota-met factory is exactly when the episode gets interesting.

Observations never carry disruption information (kind, seed, schedule,
fire events). The ledger is scorer-only ground truth; see
``tests/wrench/test_disruption_task.py`` for the prompt-leak test.
"""

import importlib.resources
import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fle.agents.llm.parsing import PythonParser
from fle.commons.cluster_ips import get_local_container_ips
from fle.disruptions.scoring import (
    detection_counts,
    detection_metrics,
    floor_adjusted_throughput_retained_parts,
    frozen_baseline,
    recovery_at,
    recovery_potential,
    shaped_reward_delta,
    throughput_retained_parts,
    time_to_recovery_parts,
    winsorize_tr,
)
from fle.disruptions.trajectory import TrajectoryWriter
from fle.env import FactorioInstance
from fle.env.gym_env.action import Action
from fle.env.gym_env.environment import FactorioGymEnv
from fle.env.gym_env.observation import Observation
from fle.env.gym_env.observation_formatter import TreeObservationFormatter
from fle.env.utils.controller_loader.system_prompt_generator import (
    SystemPromptGenerator,
)
from fle.eval.tasks.disruption_task import DisruptionRecoveryTask
from fle.eval.tasks.task_definitions.task_registry import create_task

logger = logging.getLogger(__name__)

INITIAL_FEEDBACK = (
    "The map is empty. Analyze the current game state and begin "
    "building toward the objective."
)
NO_CODE_FEEDBACK = (
    "Your reply contained no ```python block. Reply with exactly one code block."
)
NO_OUTPUT_FEEDBACK = (
    "Your reply produced no visible output (likely spent its full token "
    "budget on reasoning). Reply with a shorter, more direct ```python "
    "code block."
)

# --------------------------------------------------------------------------
# Server plumbing shared by every driver
# --------------------------------------------------------------------------


def available_container_count() -> int:
    """How many Factorio servers ``connect_instance`` can route a run_idx to.

    Must agree with ``connect_instance``'s own address resolution: a single
    external server (FACTORIO_SERVER_ADDRESS/PORT) is capacity 1; otherwise
    it's however many local ``factorio_*`` containers actually exist.
    ``get_simple_server_pool`` defaults to max_servers=32 (a cloud-scale
    default) -- calling it with no args handed out run_idx values up to 31
    against 3 real containers, and every allocation past 2 failed instantly
    at the bounds check below. Real money was spent on episodes that never
    got past this.
    """
    if os.getenv("FACTORIO_SERVER_ADDRESS") or os.getenv("FACTORIO_SERVER_PORT"):
        return 1
    _ips, _udp_ports, tcp_ports = get_local_container_ips()
    return len(tcp_ports)


async def wrench_server_pool():
    """The process-wide ``SimpleServerPool`` sized to the real container count.

    Honors ``FLE_SERVER_START`` / ``FLE_SERVER_END`` (a sub-range of the
    containers, for partitioning one cluster across several drivers) via
    ``get_simple_server_pool``.
    """
    from fle.eval.inspect.integration.simple_server_pool import (
        get_simple_server_pool,
    )

    return await get_simple_server_pool(max_servers=available_container_count())


def connect_instance(
    run_idx: Optional[int] = None,
    address: Optional[str] = None,
    tcp_port: Optional[int] = None,
) -> FactorioInstance:
    """Connect to one Factorio server and put it at speed 10, unpaused.

    Resolution order mirrors ``fle.env.gym_env.registry.make_factorio_env``:
    an explicit ``tcp_port`` wins; else ``FACTORIO_SERVER_ADDRESS`` /
    ``FACTORIO_SERVER_PORT`` (a single external server); else the local
    container list indexed by ``PORT_OFFSET + run_idx``.
    """
    if tcp_port is None:
        address = address or os.getenv("FACTORIO_SERVER_ADDRESS")
        tcp_port = os.getenv("FACTORIO_SERVER_PORT")
        if not address and not tcp_port:
            if run_idx is None:
                raise ValueError("connect_instance needs run_idx or tcp_port")
            ips, _udp_ports, tcp_ports = get_local_container_ips()
            idx = int(os.environ.get("PORT_OFFSET", 0)) + run_idx
            if idx >= len(tcp_ports):
                raise RuntimeError(
                    f"Container index {idx} exceeds available containers "
                    f"({len(tcp_ports)})"
                )
            address, tcp_port = ips[idx], tcp_ports[idx]
    instance = FactorioInstance(
        address=address or "localhost",
        tcp_port=int(tcp_port),
        num_agents=1,
        fast=True,
        cache_scripts=True,
        inventory={},
        all_technologies_researched=False,
    )
    instance.set_speed_and_unpause(10)
    return instance


def game_tick(instance: FactorioInstance) -> int:
    """Real game.tick (never FLE's synthetic elapsed-ticks accumulator)."""
    return int(
        instance.rcon_client.send_command("/silent-command rcon.print(game.tick)")
    )


# --------------------------------------------------------------------------
# Prompts and parsing
# --------------------------------------------------------------------------


def api_reference_prompt() -> str:
    """The FLE tool/entity API reference (identical to
    ``FactorioInstance.get_system_prompt()`` for a single agent, but needs
    no live server, so datasets can be built offline)."""
    generator = SystemPromptGenerator(str(importlib.resources.files("fle") / "env"))
    return generator.generate_for_agent(agent_idx=0, num_agents=1)


def system_prompt_for(task: DisruptionRecoveryTask, trajectory_length: int) -> str:
    """API reference + task objective + the one-code-block-per-turn contract."""
    return (
        f"{api_reference_prompt()}\n\n"
        f"## TASK OBJECTIVE\n{task.goal_description}\n\n"
        f"You have {trajectory_length} trajectory steps. Each step, "
        f"reply with ONE ```python code block containing the next "
        f"program to run; its output comes back as your next "
        f"observation. Keep production running for the whole episode."
    )


def parse_code(text: Optional[str]) -> Optional[str]:
    """Extract the program from a model reply, or None when there is none.

    Same extractor the Inspect solver runs on its ``ModelOutput``
    (``fle.agents.llm.parsing.PythonParser.extract_code``): a valid-Python
    reply is taken whole, otherwise all fenced blocks are combined, with a
    last-resort chunk salvage.
    """
    if not text or not text.strip():
        return None
    try:
        result = PythonParser.extract_code(SimpleNamespace(text=text))
    except Exception as exc:
        logger.warning(f"code extraction failed: {exc}")
        return None
    if result is None:
        return None
    code, _original = result
    return code or None


# --------------------------------------------------------------------------
# Post-hoc metrics (the exact computation behind the Inspect scorers)
# --------------------------------------------------------------------------


def fired_events(ledger_events: List[dict]) -> List[dict]:
    return [e for e in ledger_events if e.get("event") == "fired"]


def tracked_item(samples: List[dict], fallback: str) -> str:
    """Tracked item name; the engine's sample counts key is authoritative."""
    for sample in reversed(samples):
        counts = sample.get("counts") or {}
        if counts:
            return next(iter(counts))
    return fallback


def _fire_summary(fire: dict) -> dict:
    return {
        "kind": fire.get("kind"),
        "tick": fire.get("tick"),
        "seed": fire.get("seed"),
    }


def episode_metrics(
    samples: List[dict],
    ledger_events: List[dict],
    quota_item: str,
    end_tick: int,
) -> Dict[str, Any]:
    """Every post-hoc WRENCH number for one episode, from raw episode data.

    Returns three blocks mirroring the Inspect scorers in
    ``fle.eval.inspect.integration.wrench_scorers`` (``throughput_retained``,
    ``recovery``, ``detection`` -- each ``{"value", "metadata"}``; those
    scorers call this function, so the two paths cannot drift), plus
    ``time_to_recovery`` (per-fire ``(ticks, recovered)`` survival data;
    see ``scoring.time_to_recovery_parts`` for why it is not folded into
    ``recovery``) and a flat ``scalars`` dict for consumers that want one
    number per metric.

    Denominator policy (mirrors ``fle.disruptions.scoring``): a metric that
    is not scoreable for this episode (no fires, degenerate baseline) has
    ``value None`` and ``metadata["scoreable"] False``. Cross-episode
    aggregation must pool the raw numerators/denominators carried in
    metadata (sum numerators / sum denominators), never average per-episode
    ratios -- ``scripts/run_table.py`` does exactly that.
    """
    fires = fired_events(ledger_events)
    item = tracked_item(samples, quota_item)

    # -- Throughput Retained, pooled over fires ----------------------------
    per_fire_tr = []
    num = 0.0
    den = 0.0
    floor_num = 0.0
    floor_den = 0.0
    floor_adjusted_num_fires = 0
    for fire in fires:
        fire_tick = int(fire.get("tick", 0))
        horizon = max(0, end_tick - fire_tick)
        parts = throughput_retained_parts(samples, item, fire_tick, horizon)
        entry = _fire_summary(fire)
        entry["horizon_ticks"] = horizon
        entry["baseline_per_min"] = frozen_baseline(samples, item, fire_tick)
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

        floor_parts = floor_adjusted_throughput_retained_parts(
            samples, item, fire_tick, horizon, fire
        )
        if floor_parts is not None:
            floor_actual, floor_expected = floor_parts
            entry["floor_actual"] = floor_actual
            entry["floor_expected"] = floor_expected
            entry["floor_tr"] = winsorize_tr(floor_actual / floor_expected)
            floor_num += floor_actual
            floor_den += floor_expected
            floor_adjusted_num_fires += 1
        else:
            entry["floor_actual"] = None
            entry["floor_expected"] = None
            entry["floor_tr"] = None
        per_fire_tr.append(entry)

    scoreable = den > 0
    pooled: Optional[float] = winsorize_tr(num / den) if scoreable else None
    floor_scoreable = floor_den > 0
    floor_pooled: Optional[float] = (
        winsorize_tr(floor_num / floor_den) if floor_scoreable else None
    )
    throughput_retained = {
        "value": pooled,
        "metadata": {
            "scoreable": scoreable,
            "item": item,
            "num_fires": len(fires),
            "pooled_numerator": num,
            "pooled_denominator": den,
            "floor_adjusted_scoreable": floor_scoreable,
            "floor_adjusted_num_fires": floor_adjusted_num_fires,
            "floor_adjusted_pooled": floor_pooled,
            "floor_adjusted_pooled_numerator": floor_num,
            "floor_adjusted_pooled_denominator": floor_den,
            "fires": per_fire_tr,
        },
    }

    # -- Recovery at budget (budget = remaining episode) ---------------------
    per_fire_recovery = []
    ttr_per_fire = []
    recovered = 0
    scoreable_fires = 0
    for fire in fires:
        fire_tick = int(fire.get("tick", 0))
        budget = max(0, end_tick - fire_tick)
        result = recovery_at(samples, item, fire_tick, budget)
        entry = _fire_summary(fire)
        entry["budget_ticks"] = budget
        entry["recovered"] = result
        per_fire_recovery.append(entry)
        if result is not None:
            scoreable_fires += 1
            if result:
                recovered += 1
        ttr_entry = _fire_summary(fire)
        ttr_entry["parts"] = time_to_recovery_parts(samples, item, fire_tick, budget)
        ttr_per_fire.append(ttr_entry)

    recovery_scoreable = scoreable_fires > 0
    rate = recovered / scoreable_fires if recovery_scoreable else None
    recovery = {
        "value": rate,
        "metadata": {
            "scoreable": recovery_scoreable,
            "item": item,
            "num_fires": len(fires),
            "recovered": recovered,
            "scoreable_fires": scoreable_fires,
            "fires": per_fire_recovery,
        },
    }

    ttr_scoreable = [e["parts"] for e in ttr_per_fire if e["parts"] is not None]
    time_to_recovery = {
        # Mean over scoreable fires, right-censored at each fire's budget.
        # A one-episode display number only: pool the per-fire (ticks,
        # recovered) pairs with a survival estimator across episodes.
        "mean_ticks": (
            sum(p["ticks"] for p in ttr_scoreable) / len(ttr_scoreable)
            if ttr_scoreable
            else None
        ),
        "fires": ttr_per_fire,
    }

    # -- Detection ---------------------------------------------------------
    det = detection_metrics(ledger_events, fires)
    counts = detection_counts(ledger_events, fires)
    latencies = det["latencies"]
    mean_latency = sum(latencies) / len(latencies) if latencies else None
    detection = {
        "value": det["recall"],
        "metadata": {
            "precision": det["precision"],
            "precision_strict": det["precision_strict"],
            "recall": det["recall"],
            "latencies": latencies,
            "mean_latency_ticks": mean_latency,
            **{
                k: counts[k]
                for k in (
                    "matched_reports",
                    "matched_reports_strict",
                    "num_reports",
                    "matched_fires",
                    "num_fires",
                )
            },
        },
    }

    scalars = {
        "throughput_retained": pooled,
        "throughput_retained_raw": num / den if scoreable else None,
        "throughput_retained_floor_adj": floor_pooled,
        "tr_scoreable": scoreable,
        "tr_pooled_numerator": num,
        "tr_pooled_denominator": den,
        "recovery_rate": rate,
        "recovery_scoreable_fires": scoreable_fires,
        "recovery_recovered": recovered,
        "time_to_recovery_ticks": time_to_recovery["mean_ticks"],
        "detection_recall": det["recall"],
        "detection_precision_strict": det["precision_strict"],
        "detection_precision": det["precision"],
        "detection_latency_ticks": mean_latency,
        "num_fires": len(fires),
        "num_reports": counts["num_reports"],
    }

    return {
        "item": item,
        "num_fires": len(fires),
        "throughput_retained": throughput_retained,
        "recovery": recovery,
        "time_to_recovery": time_to_recovery,
        "detection": detection,
        "scalars": scalars,
    }


# --------------------------------------------------------------------------
# The episode
# --------------------------------------------------------------------------


class WrenchEpisode:
    """One sentinel-task episode against one Factorio server.

    Construction is cheap and offline (task registry only). ``start()``
    connects and arms; the caller owns server-slot allocation (see
    ``wrench_server_pool``) and must call ``cleanup()`` in a ``finally``.

    Opt-in artefacts, both keyed by an episode name that carries a uuid
    (two episodes of the same task+seed racing in one process -- confirmed
    to happen -- must never share a file; ``EventLedger`` appends silently):

    - ``WRENCH_LEDGER_DIR``: the ground-truth ledger is written under
      ``$WRENCH_LEDGER_DIR/<episode_name>/<task_key>.jsonl`` instead of a
      temp dir.
    - ``WRENCH_TRAJECTORY_DIR``: one JSONL record per step (code, response,
      tick, counts, shaped reward) plus a ``.meta.json`` on cleanup.
    """

    def __init__(
        self,
        task_key: str,
        seed_offset: int = 0,
        *,
        run_idx: Optional[int] = None,
        address: Optional[str] = None,
        tcp_port: Optional[int] = None,
        trajectory_length: Optional[int] = None,
        ledger_dir: Optional[str] = None,
        trajectory_writer: Optional[TrajectoryWriter] = None,
    ):
        self.task_key = task_key
        self.seed_offset = int(seed_offset)
        self._run_idx = run_idx
        self._address = address
        self._tcp_port = tcp_port
        self.task: DisruptionRecoveryTask = create_task(task_key).with_seed_offset(
            self.seed_offset
        )
        self.trajectory_length = int(trajectory_length or self.task.trajectory_length)
        self.episode_name = f"{task_key}_seed{self.seed_offset}_{uuid.uuid4().hex[:10]}"

        ledger_root = os.environ.get("WRENCH_LEDGER_DIR")
        if ledger_dir is not None:
            self.task.ledger_dir = ledger_dir
        elif ledger_root:
            self.task.ledger_dir = str(Path(ledger_root) / self.episode_name)
        self.writer = (
            trajectory_writer
            if trajectory_writer is not None
            else TrajectoryWriter.for_env(self.episode_name)
        )

        self.instance: Optional[FactorioInstance] = None
        self.gym_env: Optional[FactorioGymEnv] = None
        self._engine = None

        # Drained engine data. Samples are drained incrementally: the engine
        # ring buffer holds only ~82k ticks, so an end-of-episode snapshot
        # would lose the frozen pre-disruption baseline.
        self.samples: List[dict] = []
        self.ledger_events: List[dict] = []
        # Potential-based reward-shaping account (backend-only, never
        # surfaced to the agent -- see scoring.recovery_potential /
        # shaped_reward_delta). One running account per active fire: a
        # later "fired" event with a different fire_tick resets prev_tick to
        # that fire_tick (Phi(fire_tick) is 0 by construction, reproducing
        # "phi_prev starts at 0"), so one fire's account cannot bleed into
        # the next.
        self.shaped_rewards: List[dict] = []
        self._shaped_fire_tick: Optional[int] = None
        self._shaped_prev_tick: Optional[int] = None

        self.turn = 0  # index of the upcoming step, 0-based
        self.steps_completed = 0  # steps whose program actually ran
        self.quota_met = False
        self.end_tick = 0
        self.error = ""
        self.feedback = INITIAL_FEEDBACK
        self.last_step_reward: Optional[float] = None
        self._result: Optional[Dict[str, Any]] = None
        self._writer_finalized = False
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "WrenchEpisode":
        """Connect, reset the map/inventory, arm the seeded disruptions."""
        self.instance = connect_instance(
            run_idx=self._run_idx, address=self._address, tcp_port=self._tcp_port
        )
        # setup() resets the map/inventory, arms the seeded disruptions via
        # the engine, and opens the episode ledger.
        self.task.setup(self.instance)
        self.gym_env = FactorioGymEnv(
            instance=self.instance, task=self.task, enable_vision=False
        )
        self._engine = self.instance.controllers["inject_disruption"]
        return self

    @property
    def started(self) -> bool:
        return self.gym_env is not None

    @property
    def is_done(self) -> bool:
        return self.turn >= self.trajectory_length

    def system_prompt(self) -> str:
        return system_prompt_for(self.task, self.trajectory_length)

    # -- turn protocol -----------------------------------------------------

    def observe(self) -> str:
        """Observation for the upcoming turn: last feedback + game state.

        Raises when the game state cannot be read (a broken server); the
        driver decides whether to retry, skip, or abort.
        """
        self._require_started()
        observation: Observation = self.gym_env.get_observation()
        obs_text = (
            TreeObservationFormatter(include_research=False, include_flows=False)
            .format(observation)
            .raw_str.replace("\\n", "\n")
        )
        return (
            f"{self.feedback}\n\n---\n\n"
            f"## Step {self.turn + 1}/{self.trajectory_length}\n\n"
            f"**Current Game State:**\n{obs_text}\n\n"
            f"Write ONE ```python block for your next action."
        )

    def step(self, code: Optional[str]) -> str:
        """Run one program (or the no-code turn), verify, drain.

        Returns the feedback for this step -- the program output with the
        throughput report appended by the task (never disruption info), or
        the error/no-code notice. ``observe()`` prepends it to the next
        observation.
        """
        self._require_started()
        if self.is_done:
            raise RuntimeError("episode is over: all trajectory steps consumed")
        turn = self.turn
        if not code:
            self.feedback = NO_CODE_FEEDBACK
            self._log_trajectory(turn, "", self.feedback, self.drain())
            self.turn += 1
            return self.feedback

        try:
            _obs, reward, terminated, _truncated, info = self.gym_env.step(
                Action(agent_idx=0, code=code)
            )
        except Exception as env_err:
            logger.warning(f"Environment error: {env_err}")
            self.feedback = f"Environment error: {env_err}"
            self._log_trajectory(turn, code, self.feedback, self.drain())
            self.turn += 1
            return self.feedback

        shaped_entry = self.drain()
        self.steps_completed += 1
        self.last_step_reward = reward

        # info["task_verification"] is the program output already enhanced
        # with the throughput report (never disruption info -- the ledger
        # is scorer-only ground truth).
        program_output = info.get("task_verification") or info.get(
            "result", "No output captured"
        )
        self.feedback = (
            f"## Step {turn + 1} Execution Results\n\n"
            f"**Program Output (STDOUT/STDERR):**\n"
            f"```\n{program_output}\n```"
        )
        self._log_trajectory(turn, code, self.feedback, shaped_entry)

        if terminated:
            # Quota met at this step's verification. Do NOT end the episode:
            # disruptions arm on demonstrated throughput, so the recovery
            # test starts here.
            self.quota_met = True
            logger.info(
                f"WRENCH {self.task_key}: quota met at step {turn + 1}; "
                f"episode continues"
            )
        logger.info(
            f"WRENCH {self.task_key} step {turn + 1}/{self.trajectory_length}: "
            f"reward={reward}, quota_met={self.quota_met}, "
            f"samples={len(self.samples)}, events={len(self.ledger_events)}"
        )
        self.turn += 1
        return self.feedback

    def skip_step(self, feedback: str) -> str:
        """Consume a turn on which no program could be attempted (the
        model call failed or produced no output). Still drains, so the
        sample history stays gap-free."""
        self._require_started()
        if self.is_done:
            raise RuntimeError("episode is over: all trajectory steps consumed")
        self.feedback = feedback
        self._log_trajectory(self.turn, "", feedback, self.drain())
        self.turn += 1
        return self.feedback

    def fail_step(self, feedback: str) -> str:
        """Consume a turn that died in the driver before/after the program
        (no drain, no trajectory record -- the Inspect solver's generic
        step-error path)."""
        self.feedback = feedback
        self.turn += 1
        return self.feedback

    # -- engine bookkeeping ------------------------------------------------

    def drain(self) -> Optional[dict]:
        """Drain new engine samples + events; returns this drain's
        shaped-reward entry (or None)."""
        try:
            new = self._engine.samples(
                since_tick=self.samples[-1]["tick"] if self.samples else 0
            )
            self.samples.extend(new)
            self.task._drain_events_to_ledger(self.instance)
            self.ledger_events = [e.model_dump() for e in self.task.ledger.read()]
            return self._update_shaped_reward()
        except Exception as drain_err:
            logger.warning(f"WRENCH drain failed: {drain_err}")
            return None

    def _update_shaped_reward(self) -> Optional[dict]:
        if not self.samples:
            return None
        fires = fired_events(self.ledger_events)
        if not fires:
            return None
        fire_tick = int(fires[-1].get("tick", 0))
        tick = self.samples[-1]["tick"]
        if fire_tick != self._shaped_fire_tick:
            self._shaped_fire_tick = fire_tick
            self._shaped_prev_tick = fire_tick
        delta = shaped_reward_delta(
            self.samples,
            self.task.quota_item,
            self._shaped_fire_tick,
            self._shaped_prev_tick,
            tick,
        )
        phi = recovery_potential(
            self.samples, self.task.quota_item, self._shaped_fire_tick, tick
        )
        self._shaped_prev_tick = tick
        entry = {
            "tick": tick,
            "fire_tick": self._shaped_fire_tick,
            "phi": phi,
            "delta": delta,
        }
        self.shaped_rewards.append(entry)
        return entry

    def _log_trajectory(
        self, step_idx: int, code: str, response: str, shaped_entry: Optional[dict]
    ) -> None:
        if self.writer is None:
            return
        try:
            self.writer.append_step(
                step_idx,
                code,
                response,
                self.samples[-1]["tick"] if self.samples else 0,
                self.samples[-1]["counts"] if self.samples else {},
                shaped_reward=shaped_entry,
            )
        except Exception as traj_err:
            logger.warning(f"WRENCH trajectory append failed: {traj_err}")

    # -- end of episode ----------------------------------------------------

    @property
    def fires(self) -> List[dict]:
        return fired_events(self.ledger_events)

    def finalize(self) -> Dict[str, Any]:
        """Final drain, read the end tick, compute every metric. Idempotent."""
        if self._result is not None:
            return self._result
        if self.started:
            self.drain()
            try:
                self.end_tick = game_tick(self.instance)
            except Exception as tick_err:
                logger.warning(f"Could not read final game tick: {tick_err}")
                if self.samples:
                    self.end_tick = int(self.samples[-1]["tick"])
        metrics = episode_metrics(
            self.samples, self.ledger_events, self.task.quota_item, self.end_tick
        )
        self._result = {
            "task_key": self.task_key,
            "seed_offset": self.seed_offset,
            "trajectory_length": self.trajectory_length,
            "steps_taken": self.turn,
            "steps_completed": self.steps_completed,
            "quota_met": self.quota_met,
            "quota_item": self.task.quota_item,
            "quota": float(self.task.quota),
            "end_tick": self.end_tick,
            "error": self.error,
            "samples": list(self.samples),
            "ledger_events": list(self.ledger_events),
            "shaped_rewards": list(self.shaped_rewards),
            "fires": [_fire_summary(f) for f in self.fires],
            "num_fires": len(self.fires),
            "metrics": metrics["scalars"],
            "scores": {
                "throughput_retained": metrics["throughput_retained"],
                "recovery": metrics["recovery"],
                "detection": metrics["detection"],
                "time_to_recovery": metrics["time_to_recovery"],
            },
        }
        return self._result

    def summary(self) -> str:
        return (
            f"Completed WRENCH episode {self.task_key} "
            f"(seed_offset={self.seed_offset}): {self.steps_completed} steps "
            f"executed, quota_met={self.quota_met}, "
            f"{len(self.fires)} disruption(s) fired, "
            f"{len(self.samples)} samples drained."
        )

    def cleanup(self) -> None:
        """Write the trajectory meta file and disconnect. Idempotent; never
        raises. Does not release the server slot -- the allocator does."""
        if self._closed:
            return
        self._closed = True
        if self.writer is not None and not self._writer_finalized:
            self._writer_finalized = True
            try:
                self.writer.finalize(
                    {
                        "task_key": self.task_key,
                        "seed_offset": self.seed_offset,
                        "steps_completed": self.steps_completed,
                        "quota_met": self.quota_met,
                        "num_fires": len(self.fires),
                        "error": self.error or None,
                    }
                )
            except Exception as finalize_err:
                logger.error(f"Error finalizing trajectory: {finalize_err}")
        if self.instance is not None:
            try:
                self.instance.cleanup()
            except Exception as cleanup_err:
                logger.error(f"Error cleaning up instance: {cleanup_err}")

    def _require_started(self) -> None:
        if not self.started:
            raise RuntimeError("WrenchEpisode.start() has not been called")
