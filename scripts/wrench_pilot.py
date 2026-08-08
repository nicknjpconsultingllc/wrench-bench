"""WRENCH pilot runner: drive a sentinel task with Claude via the Claude Code
CLI in headless mode (subscription auth). Dev/iteration harness only — the
published table runs through Inspect on API keys for cross-model
comparability.

Usage:
    python scripts/wrench_pilot.py --task iron_plate_sentinel --steps 8
"""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

from fle.disruptions.scoring import (
    detection_metrics,
    frozen_baseline,
    recovery_at,
    throughput_retained,
)
from fle.disruptions.trajectory import TrajectoryWriter
from fle.env.instance import FactorioInstance
from fle.eval.tasks.task_definitions.task_registry import create_task

CODE_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)

PILOT_INSTRUCTIONS = """
You are playing Factorio through a Python API. Each turn, reply with ONE
```python code block containing the next program to run. The stdout/stderr of
your program (with line numbers) comes back as your next observation. Do not
use any tools; do not write prose outside the code block except brief
reasoning. Work incrementally: a few actions per program, then observe.
"""


def call_claude(prompt: str, model: str, session_id: str | None) -> tuple[str, str]:
    cmd = ["claude", "--model", model, "-p", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(prompt)
    # FLE loads .env (placeholder API keys) into os.environ at import; an
    # ANTHROPIC_API_KEY in the child env would override subscription auth
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {out.stderr[:500]}")
    data = json.loads(out.stdout)
    return data.get("result", ""), data.get("session_id", session_id)


def extract_code(text: str) -> str | None:
    blocks = CODE_FENCE.findall(text)
    return blocks[-1].strip() if blocks else None


def game_tick(inst) -> int:
    return int(inst.rcon_client.send_command("/silent-command rcon.print(game.tick)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="iron_plate_sentinel")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--steps", type=int, default=None, help="override trajectory length")
    ap.add_argument("--port", type=int, default=27000)
    ap.add_argument("--outdir", default="pilot_runs")
    args = ap.parse_args()

    task = create_task(args.task)
    steps = args.steps or task.trajectory_length
    run_dir = Path(args.outdir) / f"{args.task}_{args.model}_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    task.ledger_dir = str(run_dir)

    inst = FactorioInstance(
        address="localhost",
        tcp_port=args.port,
        inventory=task.starting_inventory,
        all_technologies_researched=True,
    )
    engine = inst.controllers["inject_disruption"]
    task.setup_instance(inst)
    writer = TrajectoryWriter(run_dir / "trajectory.jsonl")

    system = (
        f"{PILOT_INSTRUCTIONS}\n\n# Task\n{task.goal_description}\n\n"
        f"# API reference\n{inst.get_system_prompt()}"
    )
    observation = "The map is empty. Begin."
    session_id = None
    start_tick = game_tick(inst)
    # drain samples incrementally: the engine's ring buffer (2000 x 41 ticks
    # ~= 82k ticks) evicts early-episode history long before a 600k-tick
    # episode ends, which nulls the frozen baseline in post-hoc scoring
    all_samples: list[dict] = []

    for step in range(steps):
        prompt = system + f"\n\n# Observation (step {step})\n{observation}" if step == 0 else (
            f"# Observation (step {step})\n{observation}\n\nReply with your next ```python block."
        )
        reply, session_id = call_claude(prompt, args.model, session_id)
        code = extract_code(reply)
        if not code:
            observation = "Your reply contained no ```python block. Reply with exactly one."
            print(f"[step {step}] no code block returned")
            continue
        score, _, response = inst.eval(code)
        task_response = task.verify(score, inst, {})
        observation = task.enhance_response_with_task_output(str(response), task_response)
        tick = game_tick(inst)
        new_samples = engine.samples(
            since_tick=all_samples[-1]["tick"] if all_samples else 0
        )
        all_samples.extend(new_samples)
        writer.append_step(step, code, observation, tick, {})
        tp = task_response.meta.get(task.throughput_key, 0)
        print(f"[step {step}] tick={tick - start_tick} throughput={tp} success={task_response.success}")
        if task_response.success:
            print("quota met")

    # --- post-hoc scoring -------------------------------------------------
    final = engine.samples(since_tick=all_samples[-1]["tick"] if all_samples else 0)
    samples = all_samples + final
    (run_dir / "samples.json").write_text(json.dumps(samples))
    ledger = task.ledger.read()
    fires = [e for e in ledger if e.event == "fired"]
    # the engine's tracked key is authoritative (str vs Prototype-safe)
    item = (
        next(iter(samples[-1]["counts"]))
        if samples and samples[-1].get("counts")
        else str(task.throughput_entity)
    )
    end_tick = game_tick(inst)
    summary = {"task": args.task, "model": args.model, "steps": steps, "fires": len(fires)}
    for i, f in enumerate(fires):
        horizon = end_tick - f.tick
        summary[f"fire_{i}"] = {
            "kind": f.kind,
            "tick": f.tick,
            "baseline": frozen_baseline(samples, item, f.tick),
            "TR": throughput_retained(samples, item, f.tick, horizon),
            "recovered": recovery_at(samples, item, f.tick, horizon),
        }
    summary["detection"] = detection_metrics(ledger, fires)
    writer.finalize(summary)
    print(json.dumps(summary, indent=2, default=str))
    inst.cleanup()


if __name__ == "__main__":
    main()
