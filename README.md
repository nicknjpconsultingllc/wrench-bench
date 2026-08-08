# WRENCH

**A disruption-recovery benchmark for LLM agents.**

Disruption recovery is the on-call skill: notice that a system you built is
now broken, diagnose what changed, and repair it while the damage compounds —
all without being told anything happened. Existing agent benchmarks measure
whether a model can *build* a plan; WRENCH measures whether it can *keep a
plan alive*. Factorio is the instrumentation: a deterministic physical
simulation with irreversible spatial commitments and a continuous, hard-to-game
ground-truth signal (factory throughput), where an early layout mistake or an
unnoticed fault has consequences that compound for hours.

An agent builds an automated factory to meet a production quota. Once the
factory demonstrably works, seeded faults strike it — a machine destroyed, a
belt line cut, a resource patch exhausted. Nothing is announced. The benchmark
measures, against ground truth the agent never sees:

| Metric | Question it answers |
|---|---|
| **Detection** (precision / recall / latency) | Did the agent notice, how fast, and does it report real faults or hallucinate them? Detection is an overt act: the agent declares faults via a `report_fault` tool. |
| **Throughput Retained (TR)** | How much production survived, as a pooled ratio against the factory's own frozen pre-disruption rate — 0 = did no better than abandoning it, 1 = full recovery, winsorized to [-0.5, 1.5]. |
| **Recovery rate at budget** | Did throughput return to ≥90% of baseline within a fixed game-tick budget, sustained? Reported as a proportion — no censoring pathology. |

The metrics are validated by construction before any LLM touches them: a
scripted **no-op agent** (ignores the fault) scores TR ≈ the exact production
fraction destroyed, and a scripted **oracle-repair agent** (fixes it
immediately) scores TR ≈ 1. Same seeds ⇒ same map, same victim entities, same
injection conditions.

## How it works

- **Server-side injection.** A Lua scheduler inside the Factorio process
  samples tracked-item production every 41 ticks and fires armed disruptions
  *in game time* — injection timing cannot be skewed by the Python harness,
  LLM latency, or wall-clock sleeps.
- **Precondition-gated arming.** A disruption arms only after trailing
  throughput holds ≥ a quota fraction for consecutive windows, so the
  TR denominator is bounded away from zero by construction: you cannot be
  disrupted before you have something to lose.
- **Append-only ground-truth ledger.** Every armed/fired event carries the
  real `game.tick` and the exact affected entities; agent `report_fault`
  declarations land in the same stream. Scorers read the ledger; agents never
  do (enforced by a prompt-leak test).
- **Agents are warned in category, never in schedule.** Task prompts say
  disruptions may occur and name the kinds; seeds and timing never appear in
  any agent-visible channel.

Disruption kinds are held to a **floor acceptance test**: with a no-op agent,
post-injection throughput must fall to ≤0.2× baseline over the measurement
window — a disruption the factory shrugs off by itself doesn't measure
recovery and doesn't ship. v1 kinds: `entity_destruction`, `belt_cut`,
`resource_exhaustion` (all permanent, all deterministic per seed).

## Relationship to prior work

WRENCH is a hard fork of the [Factorio Learning Environment](https://github.com/JackHopkins/factorio-learning-environment)
(Hopkins, Bakler & Khan, NeurIPS 2025 D&B — MIT license, full history
preserved). FLE contributes the environment substrate: the RCON/Lua bridge,
the Python REPL agent interface, entity serialization, and the recipe-
decomposition production score. WRENCH contributes the disruption engine, the
recovery measurement contract, detection scoring, and an
[observation-fidelity audit](audit/README.md) of the inherited environment
layer (7 upstream bugs reproduced or refuted; 3 fixed here, including alerts
that were destructive-on-read and ground items that invisibly blocked
placement).

Fault injection for LLM agents is an active 2026 subfield — the delta here is
the *substrate*, not the idea:

| Benchmark | Faults injected into | Recovery means | Physical/spatial state |
|---|---|---|---|
| [When Tools Fail](https://arxiv.org/abs/2606.05806) | tool-call APIs | retry/fallback call | no |
| [Failing Tools](https://openreview.net/forum?id=j7YsSnA64D) | tool responses | error handling | no |
| [StressWeb](https://arxiv.org/pdf/2604.16385) | web UI flows | re-navigation | no |
| [MAS-FIRE](https://arxiv.org/html/2602.19843v1) | multi-agent comms | re-coordination | no |
| [AgentProp-Bench](https://arxiv.org/html/2604.16706v1) | agent pipelines | error propagation handling | no |
| [MTTR-A](https://arxiv.org/html/2511.20663v1) | agent workflows | workflow retry (defines an agent MTTR) | no |
| **WRENCH** | **a physical factory the agent built** | **diagnose + physically rebuild under compounding loss** | **yes — irreversible layout, spatial repair, continuous production signal** |

In text/tool-call settings, "recovery" is choosing the right fallback call. In
WRENCH the agent must form a spatial diagnosis from observation, then execute
a multi-step physical repair whose cost depends on its own earlier layout
decisions, while every tick of delay is measured in lost production.

## Quickstart

Requirements: Docker, Python ≥3.10, [uv](https://docs.astral.sh/uv/). No
Factorio purchase needed — the free headless server is downloaded at image
build time (never redistributed, per [Wube's ToS](https://www.factorio.com/terms-of-service)).

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e .
fle cluster start -n 1          # one headless Factorio server (RCON :27000)
pytest tests/wrench -q          # 30 unit tests, no server needed
pytest tests/wrench -m wrench_live -q   # engine + bracketing vs the live server
```

Run a sentinel task with a Claude model driven by the Claude Code CLI
(subscription auth; dev harness):

```bash
python scripts/wrench_pilot.py --task iron_plate_sentinel --model sonnet --steps 32
```

Tasks: `iron_plate_sentinel`, `iron_gear_sentinel`, `copper_cable_sentinel`.
Published comparisons run through [Inspect AI](https://inspect.aisi.org.uk/)
with API-key providers so every model row shares one harness.

## Status

Working vertical slice (engine, tasks, scorers, fixtures, trajectory capture —
all verified against a live Factorio 2.0.73 server). In progress: pilot
calibration runs, the first multi-model comparison table, a React trajectory
viewer, and a `biter_raid` disruption family. Numbers published here will
always ship with their ledgers and trajectories for independent re-scoring.

The upstream FLE README is preserved at
[docs/FLE_UPSTREAM_README.md](docs/FLE_UPSTREAM_README.md).

## License

MIT for all code in this repository (inherited from FLE and preserved — see
[LICENSE](LICENSE)). Factorio itself, including its assets, belongs to Wube
Software; nothing from the game is redistributed here, and public artifacts
use the schematic renderer rather than extracted game sprites.
