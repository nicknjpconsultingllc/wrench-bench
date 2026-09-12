"""WRENCH Inspect task definitions.

Wires DisruptionRecoveryTask (the sentinel tasks) into the Inspect eval
path used for the published comparison table:

- ``wrench_sentinel()``: one Inspect Task covering all three sentinel tasks.
- ``iron_plate_sentinel_task()`` / ``iron_gear_sentinel_task()`` /
  ``copper_cable_sentinel_task()``: per-task variants.

The solver mirrors factorio_controlled_solver's step loop (server-pool
allocation, observation -> generate -> parse -> gym step -> feedback) but
drives the gym environment with a DisruptionRecoveryTask so:

- setup arms the seeded disruption engine (per-seed variation applied via
  DisruptionRecoveryTask.with_seed_offset from sample metadata),
- every gym step runs the fixed-window verifier, which drains engine events
  into the task's append-only ledger,
- engine production samples are drained incrementally each step into the
  WrenchData store (the engine ring buffer holds only ~82k ticks -- an
  end-of-episode snapshot would lose the frozen pre-disruption baseline;
  see scripts/wrench_pilot.py),
- the episode never terminates early on quota success: disruptions arm on
  demonstrated throughput, so a quota-met factory is exactly when the
  episode gets interesting.

Scoring happens post-hoc in integration/wrench_scorers.py over the store.
"""

import importlib.resources
import logging
import os
import traceback
import uuid
from pathlib import Path
from typing import List, Optional, Union

from inspect_ai import Task, task
from inspect_ai.agent import AgentState
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    ModelOutput,
    get_model,
)
from inspect_ai.solver import solver
from inspect_ai.util import store_as

from fle.agents.llm.parsing import parse_response
from fle.commons.cluster_ips import get_local_container_ips
from fle.disruptions.scoring import recovery_potential, shaped_reward_delta
from fle.disruptions.trajectory import TrajectoryWriter
from fle.env import FactorioInstance
from fle.env.gym_env.action import Action
from fle.env.gym_env.environment import FactorioGymEnv
from fle.env.gym_env.observation import Observation
from fle.env.gym_env.observation_formatter import TreeObservationFormatter
from fle.env.utils.controller_loader.system_prompt_generator import (
    SystemPromptGenerator,
)
from fle.eval.inspect.integration.simple_server_pool import get_simple_server_pool
from fle.eval.inspect.integration.wrench_scorers import (
    WrenchData,
    detection_scorer,
    recovery_scorer,
    throughput_retained_scorer,
)
from fle.eval.tasks.disruption_task import DisruptionRecoveryTask
from fle.eval.tasks.task_definitions.disruption.sentinel_tasks import (
    COPPER_CABLE_SENTINEL,
    DISRUPTION_TASKS,
    IRON_GEAR_SENTINEL,
    IRON_PLATE_SENTINEL,
)
from fle.eval.tasks.task_definitions.task_registry import create_task

logger = logging.getLogger(__name__)


def _available_container_count() -> int:
    """How many containers _make_instance can actually route run_idx to.

    Must agree with _make_instance's own address resolution: a single
    external server (FACTORIO_SERVER_ADDRESS/PORT) is capacity 1; otherwise
    it's however many local containers actually exist. get_simple_server_pool
    defaults to max_servers=32 (a cloud-scale default) -- calling it with no
    args handed out run_idx values up to 31 against 3 real containers, and
    every allocation past 2 failed instantly at _make_instance's own bounds
    check. Real money was spent on episodes that never got past this.
    """
    if os.getenv("FACTORIO_SERVER_ADDRESS") or os.getenv("FACTORIO_SERVER_PORT"):
        return 1
    _ips, _udp_ports, tcp_ports = get_local_container_ips()
    return len(tcp_ports)


def _make_instance(run_idx: int) -> FactorioInstance:
    """Connect to the Factorio container for run_idx.

    Mirrors fle.env.gym_env.registry.make_factorio_env: honors
    FACTORIO_SERVER_ADDRESS / FACTORIO_SERVER_PORT (single external server,
    e.g. the dev box on RCON 27002), otherwise resolves the container list
    with PORT_OFFSET + run_idx.
    """
    address = os.getenv("FACTORIO_SERVER_ADDRESS")
    tcp_port = os.getenv("FACTORIO_SERVER_PORT")
    if not address and not tcp_port:
        ips, _udp_ports, tcp_ports = get_local_container_ips()
        idx = int(os.environ.get("PORT_OFFSET", 0)) + run_idx
        if idx >= len(tcp_ports):
            raise RuntimeError(
                f"Container index {idx} exceeds available containers ({len(tcp_ports)})"
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


def _game_tick(instance: FactorioInstance) -> int:
    """Real game.tick (never FLE's synthetic elapsed-ticks accumulator)."""
    return int(
        instance.rcon_client.send_command("/silent-command rcon.print(game.tick)")
    )


@solver
def wrench_solver():
    """Drive a DisruptionRecoveryTask episode, draining WRENCH data per step."""

    async def solve(state: AgentState, *args, **kwargs) -> AgentState:
        metadata = getattr(state, "metadata", {}) or {}
        task_key = metadata.get("task_key", IRON_PLATE_SENTINEL)
        seed_offset = int(metadata.get("seed_offset", 0))
        data = store_as(WrenchData)
        data.seed_offset = seed_offset

        run_idx = None
        instance: Optional[FactorioInstance] = None
        writer: Optional[TrajectoryWriter] = None
        try:
            fle_task: DisruptionRecoveryTask = create_task(task_key).with_seed_offset(
                seed_offset
            )
            trajectory_length = int(
                metadata.get("trajectory_length") or fle_task.trajectory_length
            )
            ledger_root = os.environ.get("WRENCH_LEDGER_DIR")
            if ledger_root:
                # uuid, not int(time.time()): the 1-second resolution let two
                # episodes of the same task_key+seed_offset (e.g. different
                # models racing the same task in one eval_set startup burst
                # -- confirmed to actually happen) collide on an identical
                # ledger directory. EventLedger opens in append mode with no
                # episode identifier in LedgerEntry, so a collision silently
                # interleaves two episodes' disruption events into one file
                # with no exception raised -- corrupted scoring data that
                # looks completely normal.
                fle_task.ledger_dir = str(
                    Path(ledger_root)
                    / f"{task_key}_seed{seed_offset}_{uuid.uuid4().hex[:10]}"
                )

            # Per-step trajectory capture, opt-in via WRENCH_TRAJECTORY_DIR
            # (mirrors the WRENCH_LEDGER_DIR opt-in above). TrajectoryWriter
            # .for_env returns None -- a no-op -- when the env var is unset.
            # Same uuid-not-timestamp collision reasoning as ledger_dir above:
            # two episodes of the same task_key+seed_offset must not share a
            # trajectory file.
            episode_name = f"{task_key}_seed{seed_offset}_{uuid.uuid4().hex[:10]}"
            writer = TrajectoryWriter.for_env(episode_name)

            pool = await get_simple_server_pool(max_servers=_available_container_count())
            allocation = await pool.get_server_allocation()
            run_idx = allocation.run_idx
            logger.info(
                f"WRENCH {task_key} seed_offset={seed_offset}: allocated server "
                f"factorio_{run_idx}, {trajectory_length} steps"
            )

            instance = _make_instance(run_idx)
            # setup() resets the map/inventory, arms the seeded disruptions
            # via the engine, and opens the episode ledger.
            fle_task.setup(instance)
            gym_env = FactorioGymEnv(
                instance=instance, task=fle_task, enable_vision=False
            )
            engine = instance.controllers["inject_disruption"]

            data.quota_item = fle_task.quota_item
            data.quota = float(fle_task.quota)

            generator = SystemPromptGenerator(
                str(importlib.resources.files("fle") / "env")
            )
            base_prompt = generator.generate_for_agent(agent_idx=0, num_agents=1)
            system_prompt = (
                f"{base_prompt}\n\n"
                f"## TASK OBJECTIVE\n{fle_task.goal_description}\n\n"
                f"You have {trajectory_length} trajectory steps. Each step, "
                f"reply with ONE ```python code block containing the next "
                f"program to run; its output comes back as your next "
                f"observation. Keep production running for the whole episode."
            )
            state.messages = [ChatMessageSystem(content=system_prompt)]

            samples: List[dict] = []

            # Potential-based reward-shaping bookkeeping (backend-only,
            # never surfaced to the agent -- see fle.disruptions.scoring
            # .recovery_potential / shaped_reward_delta). One running
            # account per active fire: a later "fired" event with a
            # different fire_tick resets shaped_prev_tick to that new
            # fire_tick (Phi(fire_tick) is always 0 by construction, so this
            # reproduces "phi_prev starts at 0" without a separately stored
            # scalar) so one fire's account cannot bleed into the next.
            shaped_rewards: List[dict] = []
            shaped_fire_tick: Optional[int] = None
            shaped_prev_tick: Optional[int] = None

            def update_shaped_reward() -> Optional[dict]:
                nonlocal shaped_fire_tick, shaped_prev_tick
                if not samples:
                    return None
                fires = [e for e in data.ledger_events if e.get("event") == "fired"]
                if not fires:
                    return None
                fire_tick = int(fires[-1].get("tick", 0))
                tick = samples[-1]["tick"]
                if fire_tick != shaped_fire_tick:
                    shaped_fire_tick = fire_tick
                    shaped_prev_tick = fire_tick
                delta = shaped_reward_delta(
                    samples,
                    fle_task.quota_item,
                    shaped_fire_tick,
                    shaped_prev_tick,
                    tick,
                )
                phi = recovery_potential(
                    samples, fle_task.quota_item, shaped_fire_tick, tick
                )
                shaped_prev_tick = tick
                entry = {
                    "tick": tick,
                    "fire_tick": shaped_fire_tick,
                    "phi": phi,
                    "delta": delta,
                }
                shaped_rewards.append(entry)
                data.shaped_rewards = list(shaped_rewards)
                return entry

            def drain() -> Optional[dict]:
                """Incrementally drain engine samples + events into the
                store; returns this drain's shaped-reward entry (or None)
                for the trajectory writer."""
                try:
                    new = engine.samples(
                        since_tick=samples[-1]["tick"] if samples else 0
                    )
                    samples.extend(new)
                    fle_task._drain_events_to_ledger(instance)
                    data.samples = list(samples)
                    data.ledger_events = [
                        e.model_dump() for e in fle_task.ledger.read()
                    ]
                    return update_shaped_reward()
                except Exception as drain_err:
                    logger.warning(f"WRENCH drain failed: {drain_err}")
                    return None

            def log_trajectory_step(
                step_idx: int,
                code: str,
                response: str,
                shaped_entry: Optional[dict],
            ) -> None:
                """Append one JSONL trajectory record, when
                WRENCH_TRAJECTORY_DIR opted a writer in. No-op otherwise."""
                if writer is None:
                    return
                try:
                    writer.append_step(
                        step_idx,
                        code,
                        response,
                        samples[-1]["tick"] if samples else 0,
                        samples[-1]["counts"] if samples else {},
                        shaped_reward=shaped_entry,
                    )
                except Exception as traj_err:
                    logger.warning(f"WRENCH trajectory append failed: {traj_err}")

            feedback = (
                "The map is empty. Analyze the current game state and begin "
                "building toward the objective."
            )
            steps_completed = 0

            for step in range(trajectory_length):
                # Trim unconditionally at the top of every iteration, not just
                # on the success tail -- the no-code-block, environment-error,
                # and generic-step-error paths all used to `continue` past a
                # trim that only ran after a fully successful step, letting
                # context balloon unbounded on a rough episode (worst case
                # ~1 + 2*trajectory_length messages, each carrying a full
                # game-state dump) and inflate cost for the rest of the run.
                if len(state.messages) > 25 and state.messages[0].role == "system":
                    state.messages = [state.messages[0]] + state.messages[-24:]
                current_code = ""
                try:
                    observation: Observation = gym_env.get_observation()
                    obs_text = (
                        TreeObservationFormatter(
                            include_research=False, include_flows=False
                        )
                        .format(observation)
                        .raw_str.replace("\\n", "\n")
                    )
                    content = (
                        f"{feedback}\n\n---\n\n"
                        f"## Step {step + 1}/{trajectory_length}\n\n"
                        f"**Current Game State:**\n{obs_text}\n\n"
                        f"Write ONE ```python block for your next action."
                    )
                    state.messages.append(ChatMessageUser(content=content))

                    try:
                        state.output = await get_model().generate(
                            input=state.messages,
                            config={
                                "max_tokens": 4096,
                                # Inspect retries forever (uncapped attempt
                                # count) when both are left None -- a
                                # persistently-erroring request would hold
                                # this episode's container slot indefinitely,
                                # starving the other episodes queued on the
                                # pool. Bound it instead: a few retries with
                                # a per-attempt timeout, then fail the step
                                # and let the normal step-error retry path
                                # (below) handle it.
                                "max_retries": 5,
                                "timeout": 180,
                            },
                        )
                    except Exception as gen_err:
                        # generate() can raise directly (e.g. a
                        # non-retryable OpenRouter error) rather than
                        # returning empty choices. Same hazard as the
                        # empty-choices guard below: without a placeholder
                        # assistant turn, the user message appended just
                        # above is left dangling and unpaired, corrupting
                        # the transcript on providers that don't sanitize
                        # alternation themselves.
                        logger.warning(
                            f"WRENCH step {step + 1} generate() error: {gen_err}"
                        )
                        feedback = f"Step {step + 1} generation error: {gen_err}"
                        state.messages.append(
                            ChatMessageAssistant(
                                content="[generation failed this step]"
                            )
                        )
                        log_trajectory_step(step, current_code, feedback, drain())
                        continue
                    if not state.output.choices:
                        # A reasoning-heavy model can burn its whole
                        # max_tokens budget on reasoning before producing any
                        # visible output, coming back with an empty choices
                        # list. state.output.message (choices[0].message)
                        # would raise IndexError here -- before
                        # parse_response ever runs its own (separately
                        # unguarded) choices[0] access -- landing in the
                        # generic step-error handler with a confusing "list
                        # index out of range" message instead of the intended
                        # retry guidance. The step is still fully billed for
                        # the reasoning tokens either way; route it through
                        # the same graceful retry path as "no code block" so
                        # at least the model gets a clear, actionable nudge.
                        feedback = (
                            "Your reply produced no visible output (likely "
                            "spent its full token budget on reasoning). "
                            "Reply with a shorter, more direct ```python "
                            "code block."
                        )
                        # Unlike the no-code-block/environment-error paths
                        # (which append the real state.output.message before
                        # their own `continue`, at the line right below this
                        # block), there IS no real assistant message here --
                        # state.output.choices is empty, so state.output.message
                        # would itself raise. Without a placeholder, this
                        # `continue` leaves a dangling, unpaired user message
                        # in state.messages; combined with the unconditional
                        # top-of-loop trim, that can produce two consecutive
                        # user messages or assistant-immediately-after-system
                        # on a later trim (verified by simulation). OpenRouter's
                        # Anthropic path does not enable inspect_ai's
                        # collapse_user_messages/collapse_assistant_messages
                        # sanitization (unlike the direct Anthropic provider,
                        # which does specifically because the native API
                        # rejects non-alternating roles) -- so keep every
                        # provider's transcript strictly paired ourselves.
                        state.messages.append(
                            ChatMessageAssistant(
                                content="[no output produced this step]"
                            )
                        )
                        log_trajectory_step(step, current_code, feedback, drain())
                        continue
                    state.messages.append(state.output.message)

                    program = parse_response(state.output)
                    if not program:
                        feedback = (
                            "Your reply contained no ```python block. Reply "
                            "with exactly one code block."
                        )
                        log_trajectory_step(step, current_code, feedback, drain())
                        continue

                    current_code = program.code
                    try:
                        obs, reward, terminated, truncated, info = gym_env.step(
                            Action(agent_idx=0, code=program.code)
                        )
                    except Exception as env_err:
                        logger.warning(f"Environment error: {env_err}")
                        feedback = f"Environment error: {env_err}"
                        log_trajectory_step(step, current_code, feedback, drain())
                        continue

                    shaped_entry = drain()
                    steps_completed += 1
                    data.steps_completed = steps_completed

                    # info["task_verification"] is the program output already
                    # enhanced with the throughput report (never disruption
                    # info -- the ledger is scorer-only ground truth).
                    program_output = info.get("task_verification") or info.get(
                        "result", "No output captured"
                    )
                    feedback = (
                        f"## Step {step + 1} Execution Results\n\n"
                        f"**Program Output (STDOUT/STDERR):**\n"
                        f"```\n{program_output}\n```"
                    )
                    log_trajectory_step(step, current_code, feedback, shaped_entry)

                    if terminated:
                        # Quota met at this step's verification. Do NOT end
                        # the episode: disruptions arm on demonstrated
                        # throughput, so the recovery test starts here.
                        data.quota_met = True
                        logger.info(
                            f"WRENCH {task_key}: quota met at step {step + 1}; "
                            f"episode continues"
                        )

                    logger.info(
                        f"WRENCH {task_key} step {step + 1}/{trajectory_length}: "
                        f"reward={reward}, quota_met={data.quota_met}, "
                        f"samples={len(samples)}, events={len(data.ledger_events)}"
                    )

                except Exception as step_err:
                    logger.error(f"WRENCH step {step + 1} error: {step_err}")
                    feedback = f"Step {step + 1} error: {step_err}"

            # Final drain + episode end tick for post-fire horizons.
            drain()
            try:
                data.end_tick = _game_tick(instance)
            except Exception as tick_err:
                logger.warning(f"Could not read final game tick: {tick_err}")
                if samples:
                    data.end_tick = int(samples[-1]["tick"])

            fires = [e for e in data.ledger_events if e.get("event") == "fired"]
            state.output = ModelOutput(
                completion=(
                    f"Completed WRENCH episode {task_key} "
                    f"(seed_offset={seed_offset}): {steps_completed} steps "
                    f"executed, quota_met={data.quota_met}, "
                    f"{len(fires)} disruption(s) fired, "
                    f"{len(samples)} samples drained."
                ),
                model=metadata.get("model", "unknown"),
            )
            state.complete = True

        except Exception as e:
            error_msg = f"WRENCH solver error: {e}\n{traceback.format_exc()}"
            logger.error(error_msg)
            data.error = error_msg
            state.output = ModelOutput(
                completion=f"Error in WRENCH episode: {e}",
                model=metadata.get("model", "unknown"),
            )
        finally:
            if writer is not None:
                try:
                    writer.finalize(
                        {
                            "task_key": task_key,
                            "seed_offset": seed_offset,
                            "steps_completed": data.steps_completed,
                            "quota_met": data.quota_met,
                            "num_fires": len(
                                [
                                    e
                                    for e in data.ledger_events
                                    if e.get("event") == "fired"
                                ]
                            ),
                            "error": data.error or None,
                        }
                    )
                except Exception as finalize_err:
                    logger.error(f"Error finalizing trajectory: {finalize_err}")
            if instance is not None:
                try:
                    instance.cleanup()
                except Exception as cleanup_err:
                    logger.error(f"Error cleaning up instance: {cleanup_err}")
            if run_idx is not None:
                try:
                    pool = await get_simple_server_pool(max_servers=_available_container_count())
                    await pool.release_run_idx(run_idx)
                except Exception as release_err:
                    logger.error(f"Error releasing server: {release_err}")

        return state

    return solve


def create_wrench_task(
    task_keys: Union[str, List[str]],
    seeds: int = 1,
    trajectory_length: Optional[int] = None,
    name: Optional[str] = None,
) -> Task:
    """Build an Inspect Task over sentinel task keys x seed offsets.

    Per-seed variation adds the seed offset (0..seeds-1) to every
    DisruptionSpec seed in the task config (see
    DisruptionRecoveryTask.with_seed_offset); offset 0 is the canonical
    registry configuration. One Sample per (task_key, seed_offset).
    """
    if isinstance(task_keys, str):
        task_keys = [task_keys]
    samples = []
    for tk in task_keys:
        cfg = DISRUPTION_TASKS[tk]  # KeyError on unknown task keys
        length = int(
            trajectory_length
            or os.getenv("FLE_TRAJECTORY_LENGTH", cfg.trajectory_length)
        )
        for offset in range(seeds):
            samples.append(
                Sample(
                    input=f"Begin task: {cfg.goal_description}",
                    target="success",
                    metadata={
                        "task_key": tk,
                        "env_id": tk,
                        "task_type": "disruption_recovery",
                        "seed_offset": offset,
                        "trajectory_length": length,
                        "expected_production_score": float(cfg.quota),
                    },
                    id=f"{tk}_seed{offset}",
                )
            )
    return Task(
        dataset=samples,
        solver=wrench_solver(),
        scorer=[
            throughput_retained_scorer(),
            recovery_scorer(),
            detection_scorer(),
        ],
        name=name or (task_keys[0] if len(task_keys) == 1 else "wrench_sentinel"),
    )


@task
def wrench_sentinel(seeds: int = 1, trajectory_length: Optional[int] = None) -> Task:
    """All three WRENCH sentinel tasks (iron plate, iron gear, copper cable)."""
    return create_wrench_task(
        list(DISRUPTION_TASKS),
        seeds=seeds,
        trajectory_length=trajectory_length,
        name="wrench_sentinel",
    )


@task
def iron_plate_sentinel_task(
    seeds: int = 1, trajectory_length: Optional[int] = None
) -> Task:
    """WRENCH iron-plate sentinel task."""
    return create_wrench_task(
        IRON_PLATE_SENTINEL, seeds=seeds, trajectory_length=trajectory_length
    )


@task
def iron_gear_sentinel_task(
    seeds: int = 1, trajectory_length: Optional[int] = None
) -> Task:
    """WRENCH iron-gear sentinel task."""
    return create_wrench_task(
        IRON_GEAR_SENTINEL, seeds=seeds, trajectory_length=trajectory_length
    )


@task
def copper_cable_sentinel_task(
    seeds: int = 1, trajectory_length: Optional[int] = None
) -> Task:
    """WRENCH copper-cable sentinel task."""
    return create_wrench_task(
        COPPER_CABLE_SENTINEL, seeds=seeds, trajectory_length=trajectory_length
    )
