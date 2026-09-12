"""Tests for the Prime Intellect ``verifiers`` package
(``environments/wrench_factorio``).

The unit tests drive ``WrenchFactorioEnv`` through verifiers' own rollout
loop with a scripted model client and a fake episode (no Factorio server):
turn parsing, the no-code-block / empty-reply paths, completion after
``trajectory_length`` turns (the last program must still run), context
trimming, server-slot release on success and on failure, and the rubric
over a canned ``finalize()`` record.

``test_live_noop_policy_rollout`` (marked ``wrench_live``) runs the real
environment against the benchmark server on :27000 with a scripted no-op
policy and asserts the rubric returns numbers.

Skipped entirely when ``verifiers`` is not installed (it is not a
dependency of the main package).
"""

import asyncio
import importlib.util
import math
import os
import sys
from pathlib import Path

import pytest

vf = pytest.importorskip("verifiers")

from verifiers.types import Response, ResponseMessage, Usage  # noqa: E402

from fle.disruptions.episode import NO_CODE_FEEDBACK, NO_OUTPUT_FEEDBACK  # noqa: E402

ENV_FILE = (
    Path(__file__).resolve().parents[2]
    / "environments"
    / "wrench_factorio"
    / "wrench_factorio.py"
)


def _load_env_module():
    if "wrench_factorio" in sys.modules:
        return sys.modules["wrench_factorio"]
    spec = importlib.util.spec_from_file_location("wrench_factorio", ENV_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["wrench_factorio"] = module
    spec.loader.exec_module(module)
    return module


wf = _load_env_module()

TASK = "iron_plate_sentinel"


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class ScriptedClient(vf.Client):
    """Returns the next scripted reply on every call."""

    def __init__(self, replies):
        super().__init__(object())
        self.replies = list(replies)
        self.prompts = []

    def setup_client(self, config):
        return object()

    async def to_native_tool(self, tool):
        return tool

    async def to_native_prompt(self, messages):
        return messages, {}

    async def get_native_response(
        self, prompt, model, sampling_args, tools=None, **kwargs
    ):
        return None

    async def raise_from_native_response(self, response):
        return None

    async def from_native_response(self, response):
        return response

    async def close(self):
        return None

    async def get_response(self, prompt, model, sampling_args, tools=None, **kwargs):
        self.prompts.append(list(prompt))
        text = self.replies.pop(0) if self.replies else "```python\nprint('idle')\n```"
        return Response(
            id=f"scripted-{len(self.prompts)}",
            created=0,
            model=model,
            usage=Usage(
                prompt_tokens=0, reasoning_tokens=0, completion_tokens=0, total_tokens=0
            ),
            message=ResponseMessage(
                content=text, finish_reason="stop", is_truncated=False
            ),
        )


CANNED_RESULT = {
    "task_key": TASK,
    "seed_offset": 0,
    "trajectory_length": 3,
    "steps_taken": 3,
    "steps_completed": 2,
    "quota_met": True,
    "quota_item": "iron-plate",
    "quota": 16.0,
    "end_tick": 50_000,
    "error": "",
    "samples": [],
    "ledger_events": [],
    "shaped_rewards": [{"tick": 40_000, "fire_tick": 30_000, "phi": 0.4, "delta": 0.1}],
    "fires": [{"kind": "entity_destruction", "tick": 30_000, "seed": 11}],
    "num_fires": 1,
    "metrics": {
        "throughput_retained": 0.73,
        "throughput_retained_raw": 0.73,
        "throughput_retained_floor_adj": 0.55,
        "tr_scoreable": True,
        "tr_pooled_numerator": 146.0,
        "tr_pooled_denominator": 200.0,
        "recovery_rate": 1.0,
        "recovery_scoreable_fires": 1,
        "recovery_recovered": 1,
        "time_to_recovery_ticks": 1234.0,
        "detection_recall": 1.0,
        "detection_precision_strict": 0.5,
        "detection_precision": 1.0,
        "detection_latency_ticks": 300.0,
        "num_fires": 1,
        "num_reports": 2,
    },
    "scores": {},
}


class FakeEpisode:
    """Same surface as WrenchEpisode, no server. Records every call."""

    instances = []

    def __init__(
        self,
        task_key,
        seed_offset=0,
        *,
        run_idx=None,
        trajectory_length=None,
        fail_start=False,
    ):
        self.task_key = task_key
        self.seed_offset = seed_offset
        self.run_idx = run_idx
        self.trajectory_length = int(trajectory_length or 3)
        self.fail_start = fail_start
        self.started = False
        self.turn = 0
        self.steps_completed = 0
        self.quota_met = False
        self.shaped_rewards = []
        self.feedback = "The map is empty."
        self.calls = []
        self.cleaned_up = False
        self.finalized = False
        FakeEpisode.instances.append(self)

    def start(self):
        if self.fail_start:
            raise ConnectionError("no server")
        self.started = True
        return self

    @property
    def is_done(self):
        return self.turn >= self.trajectory_length

    def observe(self):
        return (
            f"{self.feedback}\n\n---\n\n"
            f"observation for step {self.turn + 1}/{self.trajectory_length}"
        )

    def step(self, code):
        self.calls.append(("step", code))
        if code:
            self.steps_completed += 1
            if self.turn >= 1:
                self.shaped_rewards.append(
                    {
                        "tick": 100 * self.turn,
                        "fire_tick": 100,
                        "phi": 0.1 * self.turn,
                        "delta": 0.05,
                    }
                )
            feedback = (
                f"## Step {self.turn + 1} Execution Results\n\n```\nran: {code}\n```"
            )
        else:
            feedback = NO_CODE_FEEDBACK
        self.feedback = feedback
        self.turn += 1
        return feedback

    def skip_step(self, feedback):
        self.calls.append(("skip", feedback))
        self.feedback = feedback
        self.turn += 1
        return feedback

    def finalize(self):
        self.finalized = True
        result = dict(CANNED_RESULT)
        result["steps_completed"] = self.steps_completed
        result["quota_met"] = self.steps_completed >= 2
        return result

    def cleanup(self):
        self.cleaned_up = True


class FakePool:
    def __init__(self, size=1):
        self.available = list(range(size))
        self.released = []

    async def get_run_idx(self):
        return self.available.pop(0)

    async def release_run_idx(self, run_idx):
        self.released.append(run_idx)
        self.available.append(run_idx)


def make_env(pool, steps=3, **kwargs):
    async def pool_factory():
        return pool

    return wf.WrenchFactorioEnv(
        dataset=wf.build_dataset(tasks=TASK, seeds=1, steps=steps),
        rubric=wf.build_rubric(),
        episode_factory=FakeEpisode,
        pool_factory=pool_factory,
        **kwargs,
    )


def run_rollout(env, client, sampling_args=None):
    async def _run():
        row = env.get_dataset()[0]
        state = await env.rollout(row, client, "scripted", sampling_args or {})
        await env.rubric.score_rollout(state)
        return state

    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeEpisode.instances.clear()
    yield
    FakeEpisode.instances.clear()


# --------------------------------------------------------------------------
# Unit tests (no server)
# --------------------------------------------------------------------------


def test_dataset_rows_are_tasks_times_seeds():
    ds = wf.build_dataset(
        tasks="iron_plate_sentinel,iron_gear_sentinel", seeds=2, steps=4
    )
    assert len(ds) == 4
    row = ds[0]
    assert [m["role"] for m in row["prompt"]] == ["system"]
    assert "ONE ```python code block" in row["prompt"][0]["content"]
    assert row["info"]["task_key"] == "iron_plate_sentinel"
    assert row["info"]["trajectory_length"] == 4
    assert {r["info"]["seed_offset"] for r in ds} == {0, 1}
    with pytest.raises(ValueError):
        wf.build_dataset(tasks="not_a_task")
    assert len(wf.build_dataset()) == 6


def test_load_environment_defaults():
    env = wf.load_environment(tasks=TASK, steps=2)
    assert isinstance(env, vf.MultiTurnEnv)
    assert env.max_turns == -1  # completion comes from final_env_response
    assert env.sampling_args["max_tokens"] == wf.DEFAULT_MAX_TOKENS
    # MultiTurnEnv appends its own monitor rubric (num_turns), making a group.
    assert isinstance(env.rubric, vf.RubricGroup)
    wrench_rubric = env.rubric.rubrics[0]
    names = wrench_rubric._get_reward_func_names()
    assert names[0] == "throughput_retained"
    assert wrench_rubric.weights[0] == 1.0
    assert set(wrench_rubric.weights[1:]) == {0.0}
    assert "detection_precision_strict" in names and "quota_met" in names
    assert "num_turns" in env.rubric._get_reward_func_names()


def test_turn_parsing_and_completion():
    pool = FakePool()
    env = make_env(pool, steps=3)
    client = ScriptedClient(
        [
            "Let me look around.\n```python\nprint(inspect_inventory())\n```",
            "```python\n```",  # empty block: no program
            "```python\nsleep(1)\n```",  # last turn: must still execute
        ]
    )
    state = run_rollout(env, client)

    episode = FakeEpisode.instances[0]
    assert episode.calls == [
        ("step", "print(inspect_inventory())"),
        ("step", None),
        ("step", "sleep(1)"),
    ]
    assert episode.is_done and episode.finalized and episode.cleaned_up
    assert state["stop_condition"] == "has_final_env_response"
    assert state["is_completed"] and state["error"] is None
    assert len(state["trajectory"]) == 3
    assert pool.released == [0]
    assert state.get("wrench_episode") is None
    assert state["wrench"]["steps_completed"] == 2

    # First model call: system + the live initial observation, nothing else.
    first_prompt = client.prompts[0]
    assert [m.role for m in first_prompt] == ["system", "user"]
    assert "observation for step 1/3" in first_prompt[1].content
    # No-code turn feeds the standard notice back.
    assert NO_CODE_FEEDBACK in client.prompts[2][-1].content
    # Per-turn records.
    extras = [s["extras"]["wrench"] for s in state["trajectory"]]
    assert [e["had_program"] for e in extras] == [True, False, True]
    assert [e["program_ran"] for e in extras] == [True, False, True]
    assert extras[2]["shaped_reward"]["delta"] == 0.05
    assert all(s["reward"] is None for s in state["trajectory"])  # opt-in only
    # Completion is the full conversation: 3 assistant turns, 3 env replies.
    roles = [m.role for m in state["completion"]]
    assert roles == ["assistant", "user"] * 3
    assert "Episode complete" in state["completion"][-1].content

    # Rubric from the (canned) finalize record.
    assert state["reward"] == pytest.approx(0.73)
    m = state["metrics"]
    assert m["throughput_retained"] == pytest.approx(0.73)
    assert m["throughput_retained_floor_adj"] == pytest.approx(0.55)
    assert m["tr_scoreable"] == 1.0
    assert m["tr_pooled_denominator"] == 200.0
    assert m["recovery_rate"] == 1.0
    assert m["time_to_recovery_ticks"] == 1234.0
    assert m["detection_recall"] == 1.0
    assert m["detection_precision_strict"] == 0.5
    assert m["detection_latency_ticks"] == 300.0
    assert m["num_fires"] == 1.0
    assert m["quota_met"] == 1.0
    assert m["steps_completed"] == 2.0
    assert m["program_rate"] == pytest.approx(2 / 3)
    assert m["num_turns"] == 3


def test_empty_reply_is_a_skipped_step():
    pool = FakePool()
    env = make_env(pool, steps=2)
    client = ScriptedClient(["", "```python\nprint(1)\n```"])
    state = run_rollout(env, client)
    episode = FakeEpisode.instances[0]
    assert episode.calls[0] == ("skip", NO_OUTPUT_FEEDBACK)
    assert episode.calls[1] == ("step", "print(1)")
    assert NO_OUTPUT_FEEDBACK in client.prompts[1][-1].content
    assert state["stop_condition"] == "has_final_env_response"


def test_bare_prose_runs_as_a_comment_program():
    """FLE's parser salvage: a prose-only reply becomes a comment-only
    program, which still executes (and triggers verification) -- the same
    thing the Inspect solver does with it."""
    env = make_env(FakePool(), steps=1)
    client = ScriptedClient(["I will think about it first."])
    run_rollout(env, client)
    assert FakeEpisode.instances[0].calls == [
        ("step", "# I will think about it first.")
    ]


def test_shaped_turn_rewards_opt_in():
    env = make_env(FakePool(), steps=3, shaped_turn_rewards=True)
    client = ScriptedClient(["```python\nprint(1)\n```"] * 3)
    state = run_rollout(env, client)
    rewards = [s["reward"] for s in state["trajectory"]]
    assert rewards == [0.0, 0.05, 0.05]


def test_context_window_trims_like_the_inspect_harness():
    env = make_env(FakePool(), steps=5, max_context_messages=2)
    client = ScriptedClient(["```python\nprint(%d)\n```" % i for i in range(5)])
    state = run_rollout(env, client)
    # Turn k's prompt: system + at most (2 + 1) trailing messages.
    lengths = [len(p) for p in client.prompts]
    assert lengths == [2, 4, 4, 4, 4]
    for prompt in client.prompts[1:]:
        assert prompt[0].role == "system"
        assert [m.role for m in prompt[1:]] == ["user", "assistant", "user"]
    # Turn 3 has dropped the first observation/reply pair.
    assert "observation for step 2/5" in client.prompts[2][1].content
    # The full conversation is still rendered.
    assert len(state["completion"]) == 10


def test_start_failure_releases_the_slot_and_records_an_error():
    pool = FakePool()

    def failing_factory(*args, **kwargs):
        return FakeEpisode(*args, fail_start=True, **kwargs)

    async def pool_factory():
        return pool

    env = wf.WrenchFactorioEnv(
        dataset=wf.build_dataset(tasks=TASK, seeds=1, steps=2),
        rubric=wf.build_rubric(),
        episode_factory=failing_factory,
        pool_factory=pool_factory,
    )
    client = ScriptedClient([])
    state = run_rollout(env, client)
    assert client.prompts == []  # never called the model
    assert state["stop_condition"] == "has_error"
    assert isinstance(state["error"], vf.InfraError)
    assert pool.released == [0]
    assert FakeEpisode.instances[0].cleaned_up
    assert state["reward"] == 0.0
    assert state["metrics"]["tr_scoreable"] == 0.0


def test_rubric_defaults_when_unscoreable():
    state = {
        "wrench": {"metrics": {"throughput_retained": None, "detection_recall": 1.0}}
    }
    assert wf.throughput_retained(state) == 0.0
    assert wf.tr_scoreable(state) == 0.0
    assert wf.detection_recall(state) == 1.0
    assert wf.quota_met({"wrench": None}) == 0.0
    assert wf.steps_completed({}) == 0.0
    assert wf.program_rate({"trajectory": []}) == 0.0


# --------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------


@pytest.mark.wrench_live
def test_live_noop_policy_rollout(monkeypatch):
    """Three real turns on :27000 with a scripted no-op policy: the rubric
    returns finite numbers, the slot is released, no fire (sparse-signal
    property: reward 0.0 with tr_scoreable 0)."""
    from tests.wrench.conftest import WRENCH_RCON_PORT

    from fle.eval.inspect.integration import simple_server_pool

    monkeypatch.setenv("FACTORIO_SERVER_ADDRESS", "localhost")
    monkeypatch.setenv("FACTORIO_SERVER_PORT", str(WRENCH_RCON_PORT))
    monkeypatch.delenv("FLE_SERVER_START", raising=False)
    monkeypatch.delenv("FLE_SERVER_END", raising=False)
    monkeypatch.setattr(simple_server_pool, "_simple_server_pool", None)

    env = wf.load_environment(tasks=TASK, steps=3)
    client = ScriptedClient(
        [
            "```python\nprint(inspect_inventory())\n```",
            "```python\n```",  # empty block: no program this turn
            "```python\nsleep(1)\n```",
        ]
    )
    state = run_rollout(env, client)

    assert state["error"] is None, state["error"]
    assert state["stop_condition"] == "has_final_env_response"
    assert len(state["trajectory"]) == 3
    result = state["wrench"]
    assert result["steps_completed"] == 2
    assert result["steps_taken"] == 3
    assert result["num_fires"] == 0
    assert result["end_tick"] > 0
    assert result["samples"], "engine samples must have been drained"

    first_obs = client.prompts[0][1].content
    assert "Step 1/3" in first_obs and "Current Game State" in first_obs
    second_obs = client.prompts[1][-1].content
    assert "Step 1 Execution Results" in second_obs
    assert "Step 2/3" in second_obs
    assert NO_CODE_FEEDBACK in client.prompts[2][-1].content
    lowered = " ".join(
        m.content for p in client.prompts for m in p if m.role == "user"
    ).lower()
    for forbidden in ("disruption seed", "fire_tick", "ledger", "armed"):
        assert forbidden not in lowered

    metrics = state["metrics"]
    for name, value in metrics.items():
        assert isinstance(value, float) and math.isfinite(value), (name, value)
    assert state["reward"] == 0.0
    assert metrics["tr_scoreable"] == 0.0
    assert metrics["num_fires"] == 0.0
    assert metrics["detection_recall"] == 1.0
    assert metrics["steps_completed"] == 2.0
    assert metrics["program_rate"] == pytest.approx(2 / 3)
    assert metrics["num_turns"] == 3.0

    pool = asyncio.run(simple_server_pool.get_simple_server_pool(max_servers=1))
    assert pool.get_allocated_count() == 0
    assert os.environ["FACTORIO_SERVER_PORT"] == str(WRENCH_RCON_PORT)
