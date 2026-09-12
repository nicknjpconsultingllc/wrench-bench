"""WRENCH as a Prime Intellect ``verifiers`` environment.

WRENCH measures disruption recovery: the agent builds an automated Factorio
factory to a production quota, seeded faults strike it once it demonstrably
works (nothing is announced), and the benchmark scores -- against ground
truth the agent never sees -- how much throughput survived (Throughput
Retained), whether production recovered, and whether the agent noticed and
reported the fault.

This module is a thin ``MultiTurnEnv`` around
``fle.disruptions.episode.WrenchEpisode``, the same episode driver the
repository's Inspect solver uses, so the two harnesses produce identical
observations and identical metrics.

Turn protocol (one turn == one WRENCH step):

    system: FLE Python API reference + task objective + "reply with ONE
            ```python block" (from the dataset row)
    user:   initial observation (game state, appended in ``setup_state``)
    assistant: ```python ... ```
    user:   program output + throughput report + next game state
    ...
    user (final): the last step's program output; episode over

The final model turn's program is executed too: verifiers checks stop
conditions *before* it would call ``env_response``, so ``max_turns`` is
deliberately left unset and completion is signalled with
``state["final_env_response"]`` from inside ``env_response`` once the
episode has consumed ``trajectory_length`` steps.

A live Factorio headless server is required (``fle cluster start -n 1``,
RCON on :27000). Rollouts are bound to servers through the repository's
``SimpleServerPool``; concurrency beyond the container count simply waits
for a free slot.
"""

import asyncio
import logging
from typing import Any, Callable, Optional

import verifiers as vf
from datasets import Dataset
from verifiers.types import Messages, State
from verifiers.utils.message_utils import concat_messages, maybe_normalize_messages

from fle.disruptions.episode import (
    NO_OUTPUT_FEEDBACK,
    WrenchEpisode,
    parse_code,
    system_prompt_for,
    wrench_server_pool,
)
from fle.eval.tasks.task_definitions.disruption.sentinel_tasks import (
    DISRUPTION_TASKS,
)
from fle.eval.tasks.task_definitions.task_registry import create_task

logger = logging.getLogger(__name__)

# The Inspect harness's per-step generation budget; ``vf-eval -t`` overrides.
DEFAULT_MAX_TOKENS = 4096
# The Inspect harness keeps the system message plus the most recent 24
# messages in context (each observation carries a full game-state dump).
DEFAULT_MAX_CONTEXT_MESSAGES = 24


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


class WrenchFactorioEnv(vf.MultiTurnEnv):
    """One WRENCH sentinel-task episode per rollout.

    Rollout-local objects live in ``state``:

    - ``state["wrench_episode"]``: the live ``WrenchEpisode`` (dropped at
      cleanup);
    - ``state["wrench_run_idx"]``: the pool slot, released at cleanup;
    - ``state["wrench"]``: ``WrenchEpisode.finalize()`` -- samples, ledger
      events, fires, quota_met, end_tick, shaped rewards, and every metric
      -- populated when the episode ends (or at cleanup on an abnormal exit).
      Pass ``-C wrench`` to ``vf-eval`` to save it with the results.
    - ``state["trajectory"][i]["extras"]["wrench"]``: per-turn record
      (step index, whether a program ran, the reward-shaping entry).
    """

    def __init__(
        self,
        *,
        max_context_messages: int = DEFAULT_MAX_CONTEXT_MESSAGES,
        shaped_turn_rewards: bool = False,
        episode_factory: Callable[..., WrenchEpisode] = WrenchEpisode,
        pool_factory: Callable[[], Any] = wrench_server_pool,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.max_context_messages = int(max_context_messages)
        self.shaped_turn_rewards = bool(shaped_turn_rewards)
        self._episode_factory = episode_factory
        self._pool_factory = pool_factory

    # -- rollout lifecycle -------------------------------------------------

    async def setup_state(self, state: State, **kwargs) -> State:
        info = state.get("info") or {}
        task_key = info["task_key"]
        seed_offset = int(info.get("seed_offset", 0))
        trajectory_length = info.get("trajectory_length") or None

        state["wrench"] = None
        state["wrench_episode"] = None
        state["wrench_env_responses"] = []

        pool = await self._pool_factory()
        run_idx = await pool.get_run_idx()
        state["wrench_run_idx"] = run_idx
        try:
            episode = self._episode_factory(
                task_key,
                seed_offset,
                run_idx=run_idx,
                trajectory_length=trajectory_length,
            )
            state["wrench_episode"] = episode
            await asyncio.to_thread(episode.start)
            initial_observation = await asyncio.to_thread(episode.observe)
        except Exception as exc:
            # The cleanup handler releases the slot and closes the episode.
            raise vf.InfraError(
                f"WRENCH {task_key} seed_offset={seed_offset}: could not start "
                f"episode on server slot {run_idx}: {exc}"
            ) from exc
        logger.info(
            f"WRENCH {task_key} seed_offset={seed_offset}: server slot {run_idx}, "
            f"{episode.trajectory_length} steps"
        )
        state["prompt"] = concat_messages(
            [state["prompt"], [vf.UserMessage(content=initial_observation)]]
        )
        return state

    async def env_response(
        self, messages: Messages, state: State, **kwargs
    ) -> Messages:
        episode: WrenchEpisode = state["wrench_episode"]
        step_index = episode.turn
        reply = self.parser._content_to_text(
            self.parser._message_field(messages[-1], "content")
        )
        code = parse_code(reply)
        completed_before = episode.steps_completed
        try:
            if not reply.strip():
                feedback = await asyncio.to_thread(
                    episode.skip_step, NO_OUTPUT_FEEDBACK
                )
            else:
                feedback = await asyncio.to_thread(episode.step, code)
            if episode.is_done:
                state["wrench"] = await asyncio.to_thread(episode.finalize)
                response = [
                    vf.UserMessage(
                        content=(
                            f"{feedback}\n\n---\n\n"
                            f"Episode complete: all {episode.trajectory_length} "
                            f"steps used."
                        )
                    )
                ]
                state["final_env_response"] = response
            else:
                observation = await asyncio.to_thread(episode.observe)
                response = [vf.UserMessage(content=observation)]
        except Exception as exc:
            raise vf.InfraError(
                f"WRENCH {episode.task_key} step {step_index + 1} failed: {exc}"
            ) from exc

        shaped = episode.shaped_rewards[-1] if episode.shaped_rewards else None
        step = state["trajectory"][-1]
        step["extras"]["wrench"] = {
            "step": step_index,
            "had_program": code is not None,
            "program_ran": episode.steps_completed > completed_before,
            "quota_met": episode.quota_met,
            "shaped_reward": shaped,
        }
        if self.shaped_turn_rewards:
            step["reward"] = float(shaped["delta"]) if shaped else 0.0
        state["wrench_env_responses"].append(response)
        return response

    async def get_prompt_messages(self, state: State) -> Messages:
        """Same as ``MultiTurnEnv`` but with the Inspect harness's context
        window: system message + the most recent ``max_context_messages``
        messages. Every observation carries a full game-state dump, so an
        untrimmed 32-step episode overflows most context windows and is
        billed in full every turn."""
        if len(state["trajectory"]) == 0:
            return state["prompt"]
        prev = state["trajectory"][-1]
        messages = concat_messages([prev["prompt"], prev["completion"]])
        env_response = await self.env_response(messages, state)
        env_response = maybe_normalize_messages(env_response, field_name="env_response")
        return self._trim(concat_messages([messages, env_response]))

    def _trim(self, messages: Messages) -> Messages:
        limit = self.max_context_messages
        if limit <= 0 or len(messages) <= limit + 2:
            return messages
        first_role = self.parser._message_field(messages[0], "role")
        if first_role != "system":
            return messages
        return [messages[0]] + list(messages[-(limit + 1) :])

    async def render_completion(self, state: State) -> None:
        """The full, untrimmed conversation after the prompt: each model
        turn followed by the environment's reply. Per-turn prompts in
        ``state["trajectory"]`` keep the trimmed view the model saw."""
        completion: Messages = []
        responses = state.get("wrench_env_responses") or []
        for i, step in enumerate(state["trajectory"]):
            completion.extend(step["completion"])
            if i < len(responses):
                completion.extend(responses[i])
        state["completion"] = completion

    @vf.cleanup
    async def close_episode(self, state: State) -> None:
        """Finalize on abnormal exit, disconnect, release the server slot.

        A rollout timeout cancels the loop while a step may still be running
        in its worker thread; the disconnect then races that thread. The
        slot is released regardless so the pool never leaks.
        """
        episode: Optional[WrenchEpisode] = state.get("wrench_episode")
        if episode is not None:
            if state.get("wrench") is None and episode.started:
                try:
                    state["wrench"] = await asyncio.to_thread(episode.finalize)
                except Exception as exc:
                    logger.warning(f"WRENCH finalize failed at cleanup: {exc}")
            await asyncio.to_thread(episode.cleanup)
            state["wrench_episode"] = None
        run_idx = state.pop("wrench_run_idx", None)
        if run_idx is not None:
            try:
                pool = await self._pool_factory()
                await pool.release_run_idx(run_idx)
            except Exception as exc:
                logger.error(f"Error releasing server slot {run_idx}: {exc}")


# --------------------------------------------------------------------------
# Rubric
# --------------------------------------------------------------------------


def _scalar(state: State, key: str, default: float = 0.0) -> float:
    result = state.get("wrench") or {}
    value = (result.get("metrics") or {}).get(key)
    if value is None:
        return default
    return float(value)


def throughput_retained(state: State) -> float:
    """Reward. Pooled, winsorized Throughput Retained over every fired
    disruption (sum actual / sum expected post-fire production against the
    frozen pre-fire baseline, clamped to [-0.5, 1.5]).

    0.0 when no disruption fired or none had a valid baseline -- e.g. the
    factory never met the arming precondition. Check ``tr_scoreable``
    before pooling; this is the benchmark's known sparse-signal property."""
    return _scalar(state, "throughput_retained")


def tr_scoreable(state: State) -> float:
    """1.0 when at least one fire had a valid frozen baseline."""
    return _scalar(state, "tr_scoreable")


def throughput_retained_raw(state: State) -> float:
    """Pooled TR before the winsorize clamp (can exceed 1.5 on overbuild)."""
    return _scalar(state, "throughput_retained_raw")


def throughput_retained_floor_adj(state: State) -> float:
    """TR with the passive-redundancy floor removed from numerator and
    denominator (entity_destruction fires only); 0.0 when undefined."""
    return _scalar(state, "throughput_retained_floor_adj")


def tr_pooled_numerator(state: State) -> float:
    """Raw pooled numerator, for cross-rollout pooling (sum/sum)."""
    return _scalar(state, "tr_pooled_numerator")


def tr_pooled_denominator(state: State) -> float:
    """Raw pooled denominator, for cross-rollout pooling (sum/sum)."""
    return _scalar(state, "tr_pooled_denominator")


def recovery_rate(state: State) -> float:
    """Fraction of scoreable fires whose trailing rate returned to >= 0.9x
    baseline for two consecutive samples before the episode ended."""
    return _scalar(state, "recovery_rate")


def time_to_recovery_ticks(state: State) -> float:
    """Mean ticks from fire to sustained recovery, right-censored at the
    remaining episode; 0.0 when no fire was scoreable."""
    return _scalar(state, "time_to_recovery_ticks")


def detection_recall(state: State) -> float:
    """Fraction of fires matched by a ``report_fault`` call; gated to 0
    when loose precision < 0.5; 1.0 with no fires (vacuous)."""
    return _scalar(state, "detection_recall", default=1.0)


def detection_precision_strict(state: State) -> float:
    """Reports within 3 tiles of a fired entity / all reports (1.0 with no
    reports)."""
    return _scalar(state, "detection_precision_strict", default=1.0)


def detection_precision(state: State) -> float:
    """Loose (10-tile) report precision (1.0 with no reports)."""
    return _scalar(state, "detection_precision", default=1.0)


def detection_latency_ticks(state: State) -> float:
    """Mean ticks from fire to first matching report; 0.0 when none."""
    return _scalar(state, "detection_latency_ticks")


def num_fires(state: State) -> float:
    return _scalar(state, "num_fires")


def quota_met(state: State) -> float:
    """1.0 when the factory hit the production quota at any verification."""
    result = state.get("wrench") or {}
    return 1.0 if result.get("quota_met") else 0.0


def steps_completed(state: State) -> float:
    """Turns whose program actually executed."""
    result = state.get("wrench") or {}
    return float(result.get("steps_completed") or 0)


def program_rate(state: State) -> float:
    """Fraction of model turns that yielded a runnable program. FLE's parser
    salvages bare prose into a comment-only program, so this only drops on
    empty or unparseable replies."""
    steps = state.get("trajectory") or []
    if not steps:
        return 0.0
    hits = sum(
        1 for s in steps if (s.get("extras") or {}).get("wrench", {}).get("had_program")
    )
    return hits / len(steps)


def build_rubric() -> vf.Rubric:
    rubric = vf.Rubric()
    rubric.add_reward_func(throughput_retained, weight=1.0)
    for metric in (
        tr_scoreable,
        throughput_retained_raw,
        throughput_retained_floor_adj,
        tr_pooled_numerator,
        tr_pooled_denominator,
        recovery_rate,
        time_to_recovery_ticks,
        detection_recall,
        detection_precision_strict,
        detection_precision,
        detection_latency_ticks,
        num_fires,
        quota_met,
        steps_completed,
        program_rate,
    ):
        rubric.add_metric(metric)
    return rubric


# --------------------------------------------------------------------------
# Dataset + loader
# --------------------------------------------------------------------------


def _task_keys(tasks: Any) -> list[str]:
    if tasks is None:
        return list(DISRUPTION_TASKS)
    if isinstance(tasks, str):
        tasks = [t.strip() for t in tasks.split(",") if t.strip()]
    unknown = sorted(set(tasks) - set(DISRUPTION_TASKS))
    if unknown:
        raise ValueError(
            f"Unknown WRENCH task(s) {unknown}; choose from {list(DISRUPTION_TASKS)}"
        )
    return list(tasks)


def build_dataset(
    tasks: Any = None, seeds: int = 1, steps: Optional[int] = None
) -> Dataset:
    """One row per (task_key, seed_offset).

    Seed offsets shift every disruption seed of the task (offset 0 is the
    canonical configuration), so ``seeds=3`` gives three distinct fault
    schedules per task. The system prompt is built offline from the task
    registry; the first observation is read from the live server in
    ``setup_state``.
    """
    rows = []
    for task_key in _task_keys(tasks):
        task = create_task(task_key)
        trajectory_length = int(steps or task.trajectory_length)
        system_prompt = system_prompt_for(task, trajectory_length)
        for seed_offset in range(int(seeds)):
            rows.append(
                {
                    "prompt": [{"role": "system", "content": system_prompt}],
                    "answer": "",
                    "info": {
                        "task_key": task_key,
                        "seed_offset": seed_offset,
                        "trajectory_length": trajectory_length,
                        "quota_item": task.quota_item,
                        "quota": float(task.quota),
                    },
                }
            )
    return Dataset.from_list(rows)


def load_environment(
    tasks: Any = None,
    seeds: int = 1,
    steps: Optional[int] = None,
    max_context_messages: int = DEFAULT_MAX_CONTEXT_MESSAGES,
    shaped_turn_rewards: bool = False,
    **kwargs,
) -> vf.Environment:
    """WRENCH disruption-recovery environment.

    Args:
        tasks: sentinel task key(s) -- a list or comma-separated string.
            Default: all six (``iron_plate_sentinel``, ``iron_gear_sentinel``,
            ``copper_cable_sentinel``, ``iron_plate_observability_sentinel``,
            ``iron_plate_adaptive_sentinel``, ``iron_plate_scarcity_sentinel``).
        seeds: seed offsets per task (rows = tasks x seeds).
        steps: episode length in turns; default is each task's own
            ``trajectory_length`` (32).
        max_context_messages: messages kept after the system prompt in each
            model call (0 = never trim).
        shaped_turn_rewards: write the potential-based reward-shaping delta
            of each turn into ``trajectory[i]["reward"]`` (0.0 before any
            fire). Off by default: the rollout reward (TR) is the benchmark
            signal; the deltas are always available in
            ``trajectory[i]["extras"]["wrench"]["shaped_reward"]``.
        **kwargs: forwarded to ``MultiTurnEnv`` (e.g. ``timeout_seconds``).
    """
    dataset = build_dataset(tasks=tasks, seeds=seeds, steps=steps)
    sampling_args = {"max_tokens": DEFAULT_MAX_TOKENS}
    sampling_args.update(kwargs.pop("sampling_args", None) or {})
    return WrenchFactorioEnv(
        dataset=dataset,
        eval_dataset=dataset,
        rubric=build_rubric(),
        sampling_args=sampling_args,
        max_context_messages=max_context_messages,
        shaped_turn_rewards=shaped_turn_rewards,
        **kwargs,
    )
