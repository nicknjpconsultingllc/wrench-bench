"""Per-step trajectory capture for WRENCH episodes.

A TrajectoryWriter appends one JSONL record per solver step (code executed,
environment response, real game tick, produced counts) and finalizes with a
sibling ``.meta.json`` describing the episode. Enabled in the Inspect solver
path only when the ``WRENCH_TRAJECTORY_DIR`` environment variable is set --
see ``for_env()``.
"""

import json
import os
from pathlib import Path
from typing import Optional


class TrajectoryWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any stale file from a previous run at this path.
        self.path.write_text("")

    def append_step(
        self,
        step_index: int,
        code: str,
        response: str,
        game_tick: int,
        produced_counts: dict | None = None,
        shaped_reward: dict | None = None,
    ) -> None:
        record = {
            "step_index": step_index,
            "code": code,
            "response": str(response),
            "game_tick": game_tick,
            "produced_counts": produced_counts or {},
        }
        # Optional dense reward-shaping entry (see
        # fle.disruptions.scoring.recovery_potential / shaped_reward_delta).
        # Omitted from the record entirely when absent, so existing callers
        # (wrench_pilot.py, tests/wrench/test_trajectory.py) that never pass
        # it see byte-for-byte identical records to before.
        if shaped_reward is not None:
            record["shaped_reward"] = shaped_reward
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def finalize(self, meta: dict) -> None:
        meta_path = self.path.with_suffix(".meta.json")
        with meta_path.open("w") as f:
            json.dump(meta, f, indent=2, default=str)

    @classmethod
    def for_env(
        cls, episode_name: str, env_var: str = "WRENCH_TRAJECTORY_DIR"
    ) -> Optional["TrajectoryWriter"]:
        """Writer under $WRENCH_TRAJECTORY_DIR, or None when unset (no-op)."""
        base = os.environ.get(env_var)
        if not base:
            return None
        return cls(Path(base) / f"{episode_name}.jsonl")
