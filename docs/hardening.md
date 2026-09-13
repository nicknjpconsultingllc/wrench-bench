# Scoring hardening log

Whatever a metric rewards is what a policy trained against it learns to do.
This is the record of the adversarial review WRENCH's scoring and harness went
through before and after the first paid model run: every way the numbers could
have been gamed or silently wrong that was found, how it was reproduced, and
what fixed it. Commit hashes link the diffs.

Method. Rounds of three to four independent review agents, each assigned one
lens (correctness, security, efficiency, measurement validity, RL gaming). A
finding counted only with a reproduction: against the live Factorio server
where the claim was about game state, as a unit test otherwise. Fixes went
onto isolated worktrees and merged after `tests/wrench` passed. Each round
re-audited the previous round's fixes; two entries below are bugs in earlier
fixes.

## Summary

| Round | Lens | Found | Commits |
|---|---|---|---|
| Pre-spend 1 | harness under real concurrency | 4 | `bcd7e623` |
| Pre-spend 2 | silent failure | 3 | `dd49f481` |
| Pre-spend 3 | bounded retries, test hygiene | 3 | `4c87e88e` |
| Scoring validity | detection gaming | 1 | `c57990bb` |
| Scoring validity | the ∫F redundancy term | 1, plus one design rejected before shipping | `4f3d1131` |
| Four-lens review | correctness, clarity, security, efficiency | 3 | `bb24259e`, `82914ebb`, `fd298058` |
| Post-grid | pool timeout | 1 | `224cc9e7` |

Sixteen findings, sixteen fixed. Three were reward-hacking paths, one was a
Lua encoding bug first misfiled as remote code execution (§2), six would have
produced a results table that looked clean while being mostly wrong.

## 1. Reward-hacking paths closed

### 1.1 `report_fault` spam scored like real detection

`report_fault` is a free no-op; its only cost is one of the agent's 32-48
steps. Recall counted a fire as detected if *any* report matched it. Measured
live: ten identical spam reports and one accurate report both scored
precision 1.0, recall 1.0. Worse, `detection_scorer` reported recall as the
Inspect score value; precision only ever reached metadata, so it constrained
nothing.

Fix (`wrench_core/scoring.py:detection_counts`): credit caps at one match
per fire while the report count stays uncapped, so volume suppresses precision
directly (spam precision 1.0 → 0.1); recall gates to 0 when loose-radius
precision drops below `DETECTION_PRECISION_FLOOR = 0.5`. The floor sits on the
boundary of the one legitimate double-report case in the pilot data so that
case stays ungated.

### 1.2 The redundancy floor, and the design that would have rewarded self-sabotage

The measurement contract defines TR as `(∫A−∫F)/(∫C−∫F)`, with F the
throughput a no-op agent keeps. The shipped code computed `∫A/∫C`. On
`entity_destruction` (destroys one entity) a factory with two furnaces keeps
half its throughput doing nothing, so a no-op agent scored TR≈0.5.

The obvious fix, observing the post-fire plateau to estimate F, has an
exploit: damage your own surviving furnace after the fire to push the observed
floor down, then "recover" from your own damage for credit. A reference
implementation of that design lives in
`tests/test_scoring.py::TestFloorAdjustedClosesSelfSabotageExploit` (wrench_core) and
hands ~0.49 of free credit to the sabotage trajectory.

Shipped instead (`server.lua:KINDS.entity_destruction`,
`wrench_core/scoring.py:floor_adjusted_throughput_retained_parts`): the redundancy count
is taken *before* the victim dies, from the same electric network (or a
100-tile radius for burner-tier builds), and stashed in the ledger. Nothing the
agent does after the fire can move it. The floor is `expected × (n−1)/n`; with
no redundancy it reduces to plain TR. Reported as `TR (floor-adj)` alongside
`TR` and `TR (raw)`, never replacing them.

### 1.3 Two bugs in the fixes above

The spam cap in 1.1 capped credit per fire but not per report, so one report
matching two fires produced precision 2.0. Fixed with bipartite matching
(`consumed_loose`/`consumed_strict`); `matched_reports ≤ min(fires, reports)`
by construction (`bb24259e`).

The redundancy count in 1.2 was a raw same-name match across the whole map. An
abandoned furnace 300 tiles away counted as redundancy and could swing a
neutral outcome to the −0.5 winsorize floor. Reproduced: the same episode
scored 0.011 with the correct count and −0.5 with the inflated one. Fixed with
the electric-network / bounded-radius grouping described above (`82914ebb`).

## 2. Malformed Lua string encoding in the shared tool layer (and what it isn't)

Every RCON tool call goes through `slpp.encode()` to build the Lua for
`/silent-command`. `slpp` escapes double quotes but not backslashes or
newlines, so any string argument containing `\`, `\n` or `\r` produces
invalid Lua (`C:\path` → "invalid escape sequence", multi-line text →
"unfinished string"), and a string ending in an odd number of backslashes
before a quote closes its literal early so the rest runs as Lua. Reproduced
live: a crafted `cause` string passed to `report_fault` set
`storage.wrench.INJECTED_PWNED = true`.

Fix (`fle/env/tools/controller.py:_lua_encode_safe`): pre-escape backslashes,
`\n`, `\r` at every string leaf before `slpp` sees it; `slpp`'s quote
escaping composes correctly on top. 26 regression cases in
`tests/wrench/test_lua_encoding_security.py`, run through a real Lua
interpreter (`lupa`) in the exact `pcall(storage.actions.NAME, ...)` shape
the controller emits.

**This was first written up as remote code execution. A red-team pass on
2026-09-12 showed that framing was wrong.** FLE executes the agent's Python
in-process with the `FactorioInstance` in scope (`fle/env/namespace.py`
assigns `self.instance`, and the namespace is the program's globals), so an
agent program can already run
`instance.rcon_client.send_command("/silent-command ...")` directly. Verified
live: a one-line program returned `game.tick` through that path. The string
breakout adds no capability the agent doesn't already hold, in this fork or
upstream. It is a correctness bug (silent tool failures on backslashes and
newlines) and defense in depth for any future frontend that exposes tool
arguments without exposing Python. Filed upstream as such.

The finding that survives is the bigger one: **WRENCH's ground truth is
tamper-resistant against post-fire game actions, not against a Python
program that reaches for RCON.** Nothing in the harness stops an agent from
editing `storage.wrench` or spawning items, and Prime Intellect's Aug 5 2026
Prime Agent post reports an agent doing exactly that in FLE. The 18-episode
grid was hand-checked and no model did this, but the guarantee is empirical.
A sandboxed executor (agent code runs in a container with an allowlisted
RPC and no RCON reachability) is the fix, and it's a blocker before any RL
training run, where a policy will find this path. Tracked in §6.

## 3. Silent failures in the harness

Found in the three pre-spend rounds. Each of these would have produced a
plausible-looking table with wrong numbers.

- **Pool fail-fast** (`simple_server_pool.py`, `dd49f481`). `get_run_idx`
  raised on a momentarily-empty pool instead of waiting. A momentarily-empty
  pool is the normal case with 18 episodes on 3 containers. Free mock-model run
  at the real concurrency: 50% of episodes failed instantly and were recorded
  as `success, 0 fires`.
- **Results blind to internal errors** (`scripts/run_table.py`, same commit).
  `collect_episode_rows` checked `sample.error`, which the solver never sets
  (it catches and returns normally, populating `WrenchData.error`). This is
  why the failure above was invisible. Re-checked against 8 broken episodes
  from the earlier run: all 8 now flag `error` with their traceback.
- **Ledger path collision** (`wrench.py`, `bcd7e623`). Ledger dirs were
  suffixed with `int(time.time())`. Two episodes of the same task and seed
  started one second apart in a prior run; two interleaved ledgers would raise
  nothing. Now `uuid4`.
- **Empty model response** crashed the step; **conversation trim** skipped on
  every error path so a rough episode ballooned context; **`cleanup()` joined
  every thread in the process**, stalling 15 s with 3 concurrent episodes
  (`bcd7e623`).
- **Unbounded retries** held a container slot forever on a persistently
  failing request; two **dangling-message** bugs (one introduced by the
  round-1 empty-response guard) produced consecutive user messages that
  OpenRouter's Anthropic path doesn't sanitise (`dd49f481`, `4c87e88e`).
- **Live test not marked live** silently connected to and mutated an in-use
  container when a pilot was running on its port (`4c87e88e`).

## 4. The pool-timeout incident

The first real grid lost 15 of 18 episodes, every one to `Timed out after 900s
waiting for a free server slot`. Mid-run, a spot check using `sample.error`
reported 7 of 9 task logs `success` with no errors and concluded the run was
healthy. That check had the exact blind spot fixed in §3 (it didn't read
`WrenchData.error`), and the conclusion was wrong.

Root cause: 900 s was calibrated against the per-step retry budget
(5 × 180 s), never against total episode duration under 6× container
oversubscription. The 3 episodes that succeeded took 929-1581 s each; anything
queued behind two waves legitimately waited longer than the timeout. Fix:
`POOL_WAIT_TIMEOUT_SECONDS = 14400`, with the measured numbers in the comment
(`224cc9e7`). Inspect's `--resume` couldn't recover the run because it treats
a log as clean on file-level status, which was `success`; the grid was rerun
fresh, re-paying for 3 episodes, and completed 18/18.

## 5. What the real data says about the shaped reward

`recovery_potential`/`shaped_reward_delta` (potential-based, telescoping) was
validated on one dev sample before the grid. Checked against the 18 real
episodes afterward:

- Telescoping holds on all 18 fires across the 14 episodes that fired: per
  fire, `Σ delta == Φ(end) − Φ(start)` exactly, and `Φ ∈ [0, 1]` throughout.
- Resolution is poor for single-entity disruptions: 8 of 14 episodes have ≤3
  nonzero deltas, most of them one 0→1 jump, because Φ clips at 1 and the
  baseline is the quota, so any repair that restores quota saturates in one
  step. Scarcity episodes (`resource_exhaustion`, where relocation takes many
  steps) show a real gradient: 4-23 nonzero deltas.
- 4 episodes (all GPT-5.1) never reached quota, never armed, and carry no
  signal at all.

So it is a correct potential and, as shipped, a sparse one. An unclipped Φ (to
1.5, matching TR's winsorize ceiling) and snapshot-start episodes that skip the
build phase are the next tests; until then the "dense reward" claim is
unproven on real data.

## 6. Open items

- `TR (floor-adj)` is defined for `entity_destruction` only; `belt_cut`,
  `resource_exhaustion` and `adaptive_strike` return `None` (no
  non-manipulable redundancy signal exists for them yet).
- Two seeds per cell. Two cells at or below the floor (Gemini/scarcity −0.156,
  GPT-5.1/scarcity −0.5) are unreplicated.
- The unit suite ran only by hand until `.github/workflows/wrench-tests.yml`
  landed; the inherited FLE workflows never ran against this fork.
- **Agent programs can reach RCON** (§2). No sandbox exists between the
  agent's Python and `instance.rcon_client`; benchmark integrity against a
  deliberately adversarial policy is unenforced. Required before RL use.
