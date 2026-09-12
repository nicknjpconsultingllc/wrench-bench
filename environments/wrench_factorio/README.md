# wrench-factorio

### Overview
- **Environment ID**: `wrench-factorio`
- **Short description**: Disruption recovery in Factorio. The agent writes one Python program per turn against the FLE game API to build an automated factory to a production quota; once the factory demonstrably works, seeded faults destroy machines, cut belts or exhaust ore patches without any announcement. Reward is how much production survived, measured against ground truth the agent never sees.
- **Tags**: game, multi-turn, agent, code, factorio, disruption-recovery, eval, train
- **Source**: [WRENCH repository](https://github.com/nicknjpconsultingllc/wrench-bench) (a hard fork of the [Factorio Learning Environment](https://github.com/JackHopkins/factorio-learning-environment), MIT)

### Prerequisite: a Factorio headless server

Every rollout needs its own live Factorio server; the environment does not start one. From a checkout of the WRENCH repository:

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e .                      # the fork of FLE (pulls Docker SDK, RCON client, ...)
fle cluster start -n 1                   # one headless server, RCON on localhost:27000
uv pip install -e environments/wrench_factorio --no-deps
```

`fle cluster start -n N` starts N containers; the environment discovers them through Docker and runs at most N rollouts concurrently (extra rollouts wait for a free server). The free headless server is downloaded when the Docker image is built and is never redistributed, per [Wube's terms of service](https://www.factorio.com/terms-of-service); no Factorio purchase is needed. Overrides: `FACTORIO_SERVER_ADDRESS` / `FACTORIO_SERVER_PORT` (one external server, capacity 1), `FLE_SERVER_START` / `FLE_SERVER_END` (use a sub-range of the local containers), `WRENCH_LEDGER_DIR` and `WRENCH_TRAJECTORY_DIR` (keep the ground-truth ledger and a per-step JSONL trajectory for each episode).

### Datasets
- **Primary dataset**: generated from the task registry, one row per (sentinel task, seed offset). Six sentinel tasks: `iron_plate_sentinel`, `iron_gear_sentinel`, `copper_cable_sentinel` (three baseline factories), `iron_plate_observability_sentinel` (inspection calls are metered), `iron_plate_adaptive_sentinel` (the fault strikes whatever is most load-bearing in the agent's own build), `iron_plate_scarcity_sentinel` (no starting infrastructure, scarce resources).
- **Seeds**: offset `k` shifts every disruption seed of the task by `k`; offset 0 is the canonical configuration. Same seed, same map, same victim entities, same injection conditions.
- **Split sizes**: `tasks x seeds` rows (default 6 x 1); train and eval are the same generated grid.

### Task
- **Type**: multi-turn (default 32 turns per episode, one Python program per turn)
- **Output format**: exactly one ```` ```python ```` block per reply. The program's stdout/stderr plus the factory's measured throughput come back as the next observation together with the current game state (entities, inventory). Parsing is FLE's: a reply that is bare prose is salvaged into a comment-only program and still executes (a no-op that triggers the throughput measurement); an empty reply or an empty code block gets a "no code block" notice. Either way the turn is consumed.
- **Rubric overview**: the reward is winsorized Throughput Retained pooled over every fired disruption; everything else is a zero-weight metric.

### Quickstart

```bash
fle cluster start -n 1            # if not already running
uv run vf-eval wrench-factorio -m gpt-4.1-mini -n 1 -r 1 -c 1 -a '{"tasks": "iron_plate_sentinel", "steps": 32}' -C wrench -s
```

All six tasks, three seed offsets each, on a three-container cluster:

```bash
fle cluster start -n 3
uv run vf-eval wrench-factorio -m <model> -n 18 -r 1 -c 3 -a '{"seeds": 3}' -C wrench -s
```

Keep `-c` at or below the number of containers. `-C wrench` saves the full episode record (samples, ledger, fires, per-fire breakdowns) with the results so metrics can be re-pooled offline. A 32-step episode takes 15-30 wall-clock minutes at game speed 10 (each turn runs two 60-game-second throughput measurements).

### Environment Arguments

| Arg | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `tasks` | str or list | all six | Sentinel task key(s); comma-separated string accepted |
| `seeds` | int | `1` | Seed offsets per task (rows = tasks x seeds) |
| `steps` | int | task default (32) | Turns per episode |
| `max_context_messages` | int | `24` | Messages kept after the system prompt in each model call (0 = never trim). Every observation carries a full game-state dump. |
| `shaped_turn_rewards` | bool | `false` | Write the potential-based reward-shaping delta of each turn into `trajectory[i]["reward"]`. The deltas are always in `trajectory[i]["extras"]["wrench"]["shaped_reward"]`. |
| `timeout_seconds` | float | none | Per-rollout wall-clock timeout (`MultiTurnEnv`) |

### Metrics

| Metric | Meaning |
| ------ | ------- |
| `reward` = `throughput_retained` | Sum of post-fire production over sum of expected production (frozen pre-fire baseline x horizon), pooled over fires, winsorized to [-0.5, 1.5]. 0 = no better than abandoning the factory, 1 = full recovery. **0.0 when nothing fired** (see below). |
| `tr_scoreable` | 1 when at least one fire had a valid baseline. Filter on this before averaging `throughput_retained`. |
| `throughput_retained_raw` | Same ratio before the winsorize clamp (overbuild past baseline shows as > 1.5). |
| `throughput_retained_floor_adj` | TR with the passive-redundancy floor removed from numerator and denominator (`entity_destruction` fires only). |
| `tr_pooled_numerator`, `tr_pooled_denominator` | Raw pooled parts. Pool across rollouts as sum/sum, never mean-of-ratios. |
| `recovery_rate` | Fraction of scoreable fires whose trailing rate returned to >= 0.9x baseline for two consecutive samples before the episode ended. |
| `time_to_recovery_ticks` | Mean ticks from fire to sustained recovery, right-censored at the remaining episode (0 when no scoreable fire). |
| `detection_recall` | Fraction of fires the agent reported via `report_fault` within 10 tiles; gated to 0 when loose precision < 0.5; 1.0 with no fires (vacuous). |
| `detection_precision_strict` / `detection_precision` | Reports within 3 / 10 tiles of a fired entity over all reports (1.0 with no reports). |
| `detection_latency_ticks` | Mean ticks from fire to first matching report. |
| `num_fires`, `quota_met`, `steps_completed`, `program_rate`, `num_turns` | Episode bookkeeping (`program_rate` = fraction of turns that yielded a runnable program; it only drops on empty or unparseable replies). |

### Known limits

- **Sparse signal by design.** Disruptions arm only after the factory holds a fraction of the quota for consecutive measurement windows, so an agent that never builds a working factory is never disrupted, and its reward is 0 with `tr_scoreable = 0` and `num_fires = 0`. This is the benchmark's measurement contract (you cannot be disrupted before you have something to lose), so for training treat `throughput_retained` as conditional on `tr_scoreable` and use `quota_met` / `steps_completed` as the dense progress signal.
- **Pooling rule.** Averaging `throughput_retained` over rollouts is not the benchmark's headline number; pool `tr_pooled_numerator` / `tr_pooled_denominator` across seeds and fires. `vf-eval`'s `avg_metrics` are plain means.
- **Server-bound concurrency.** Rollouts beyond the container count block on the server pool (up to four hours) rather than failing. A rollout `timeout_seconds` counts that wait.
- **Prompt size.** The system prompt is the full FLE API reference (~30k tokens); with the default 24-message window, model calls run 40-80k tokens.
- **Ground truth stays hidden.** Observations never mention what was armed, when, or with which seed; the ledger is scorer-only. Do not add disruption information to the prompt.

### Versions

Built and tested against `verifiers==0.2.0` (classic `MultiTurnEnv` API) and `prime==0.6.34`. Because `prime` classifies a `verifiers>=0.2.0` floor as the v1 taskset API, publish with an explicit runtime:

```bash
prime env push --path environments/wrench_factorio --runtime v0
```
