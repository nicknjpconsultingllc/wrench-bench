"""WRENCH Inspect task definitions.

Wires DisruptionRecoveryTask (the sentinel tasks) into the Inspect eval
path used for the published comparison table:

- ``wrench_sentinel()``: one Inspect Task covering all three sentinel tasks.
- ``iron_plate_sentinel_task()`` / ``iron_gear_sentinel_task()`` /
  ``copper_cable_sentinel_task()``: per-task variants.

The solver is Inspect's message/generate loop around
``fle.disruptions.episode.WrenchEpisode``, which owns the per-step episode
logic (server connection, seeded arming via
DisruptionRecoveryTask.with_seed_offset, the fixed-window verifier, the
incremental engine drain -- the engine ring buffer holds only ~82k ticks,
so an end-of-episode snapshot would lose the frozen pre-disruption
baseline -- and the post-hoc metrics). The same class drives the Prime
Intellect ``verifiers`` package under ``environments/wrench_factorio``.

The episode never terminates early on quota success: disruptions arm on
demonstrated throughput, so a quota-met factory is exactly when the
episode gets interesting.

Scoring happens post-hoc in integration/wrench_scorers.py over the
WrenchData store, which this solver fills from the episode after every
step.
"""

import logging
import os
import traceback
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
from fle.disruptions.episode import WrenchEpisode, wrench_server_pool
from fle.eval.inspect.integration.wrench_scorers import (
    WrenchData,
    detection_scorer,
    recovery_scorer,
    throughput_retained_scorer,
)
from fle.eval.tasks.task_definitions.disruption.sentinel_tasks import (
    COPPER_CABLE_SENTINEL,
    DISRUPTION_TASKS,
    IRON_GEAR_SENTINEL,
    IRON_PLATE_SENTINEL,
)

logger = logging.getLogger(__name__)


def _sync_store(data: WrenchData, episode: WrenchEpisode) -> None:
    """Mirror the episode's drained data into the Inspect store so the
    scorers (and a partial log, if the sample dies mid-episode) see the
    latest samples/events after every step."""
    data.samples = list(episode.samples)
    data.ledger_events = list(episode.ledger_events)
    data.shaped_rewards = list(episode.shaped_rewards)
    data.steps_completed = episode.steps_completed
    data.quota_met = episode.quota_met
    data.end_tick = episode.end_tick


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
        episode: Optional[WrenchEpisode] = None
        try:
            pool = await wrench_server_pool()
            allocation = await pool.get_server_allocation()
            run_idx = allocation.run_idx

            episode = WrenchEpisode(
                task_key,
                seed_offset,
                run_idx=run_idx,
                trajectory_length=metadata.get("trajectory_length") or None,
            )
            trajectory_length = episode.trajectory_length
            logger.info(
                f"WRENCH {task_key} seed_offset={seed_offset}: allocated server "
                f"factorio_{run_idx}, {trajectory_length} steps"
            )
            episode.start()

            data.quota_item = episode.task.quota_item
            data.quota = float(episode.task.quota)
            state.messages = [ChatMessageSystem(content=episode.system_prompt())]

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
                try:
                    state.messages.append(ChatMessageUser(content=episode.observe()))

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
                        state.messages.append(
                            ChatMessageAssistant(
                                content="[generation failed this step]"
                            )
                        )
                        episode.skip_step(
                            f"Step {step + 1} generation error: {gen_err}"
                        )
                        _sync_store(data, episode)
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
                        #
                        # There IS no real assistant message here (choices is
                        # empty), so append a placeholder: without one, this
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
                        episode.skip_step(
                            "Your reply produced no visible output (likely "
                            "spent its full token budget on reasoning). "
                            "Reply with a shorter, more direct ```python "
                            "code block."
                        )
                        _sync_store(data, episode)
                        continue
                    state.messages.append(state.output.message)

                    program = parse_response(state.output)
                    episode.step(program.code if program else None)
                    _sync_store(data, episode)

                except Exception as step_err:
                    logger.error(f"WRENCH step {step + 1} error: {step_err}")
                    episode.fail_step(f"Step {step + 1} error: {step_err}")

            # Final drain + episode end tick for post-fire horizons.
            episode.finalize()
            _sync_store(data, episode)

            state.output = ModelOutput(
                completion=episode.summary(),
                model=metadata.get("model", "unknown"),
            )
            state.complete = True

        except Exception as e:
            error_msg = f"WRENCH solver error: {e}\n{traceback.format_exc()}"
            logger.error(error_msg)
            data.error = error_msg
            if episode is not None:
                episode.error = error_msg
            state.output = ModelOutput(
                completion=f"Error in WRENCH episode: {e}",
                model=metadata.get("model", "unknown"),
            )
        finally:
            if episode is not None:
                episode.cleanup()
            if run_idx is not None:
                try:
                    pool = await wrench_server_pool()
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
