# WRENCH Pilot Failure Taxonomy

A qualitative taxonomy of agent failure modes and benchmark-validity findings from WRENCH pilot runs (disruption-recovery tasks on Factorio, forked from FLE).

## 1. Method

Core data: five completed pilot runs (plus one Haiku run that crashed before step 0, leaving an empty trajectory), two models. §2.9 adds one supplementary run, collected after the V2 engine fix (recovery-chained arming), specifically because it surfaced a failure mode the original five did not. §2.10 adds three calibration runs — one per newly-added task family (observability budget, adaptive targeting, scarcity) — collected to validate each family, not to extend the core sample. §2.11 adds a 3×3 model grid (sonnet/opus/fable × the three §2.10 task families, one seed each) — cross-model signal, not a replicated result; per `docs/benchmark_design.md`'s own statistical guidance (k=5 paired seeds before trusting a cross-model gap), every claim below is explicitly flagged as N=1 pending replication.

| Run (short name) | Directory | Model | Steps | Fires |
|---|---|---|---|---|
| haiku | `iron_plate_sentinel_haiku_1786200417` | Haiku | 8 | 0 |
| sonnet-A | `iron_plate_sentinel_sonnet_1786200726` | Sonnet | 32 | 1 |
| sonnet-B | `iron_plate_sentinel_sonnet_1786205506` | Sonnet | 32 | 1 |
| gear | `iron_gear_sentinel_sonnet_1786211607` | Sonnet | 32 | 2 |
| cable | `copper_cable_sentinel_sonnet_1786211607` | Sonnet | 32 | 1 |
| gear2 (supplementary, §2.9) | `iron_gear_sentinel_sonnet_1786228879` | Sonnet | 32 | 2 |
| observability-cal (§2.10) | `iron_plate_observability_sentinel_sonnet_1786234523` | Sonnet | 32 | 1 |
| adaptive-cal (§2.10) | `iron_plate_adaptive_sentinel_sonnet_1786234523` | Sonnet | 32 | 0 (1 armed → not_applicable) |
| scarcity-cal (§2.10) | `iron_plate_scarcity_sentinel_sonnet_1786234523` | Sonnet | 48 | 2 |
| grid: scarcity × {sonnet,opus,fable} (§2.11) | `iron_plate_scarcity_sentinel_{model}_*` | all 3 | 48 | 2 each |
| grid: observability × {sonnet,opus,fable} (§2.11) | `iron_plate_observability_sentinel_{model}_*` | all 3 | 32 | 1 each |
| grid: adaptive × {sonnet,opus,fable} (§2.11) | `iron_plate_adaptive_sentinel_{model}_*` | all 3 | 32 | 1 (sonnet, fable), 2 (opus) |

Each run was hand-read: the full `trajectory.jsonl` (per-step code + environment response) coded against the task ledger (`<task>.jsonl`: armed/fired/not_applicable/report_fault events with game ticks) as ground truth, plus `trajectory.meta.json` metrics and, where present, `samples.json` production counts. Citations below are (run, step N) for trajectory evidence and (run, tick T) for ledger events.

This is a pilot-scale qualitative taxonomy from N=5 runs and 2 models. It identifies failure modes that demonstrably exist and validity bugs that demonstrably fired; it supports no quantitative claims about rates or model rankings.

## 2. Failure-mode taxonomy

### F1. Training-data contamination artifacts (2 incidents, 2 runs)

Haiku's first action imports the FLE paper's old module layout wholesale:

```python
from src.types import Prototype, Direction, Position, Resource, Technology, RecipeName
from src.tools import *
```

→ `ModuleNotFoundError: No module named 'src'` (haiku, step 0). The API is injected as globals; the import convention exists only in FLE's public repo/paper. The hallucination cost one full step (Haiku resubmitted the same ~140-line program without the imports at step 1).

A milder formatting artifact: sonnet-B emitted `</br></br></br>` HTML tags at the end of otherwise-valid Python, producing `invalid syntax` and a wasted step (sonnet-B, step 6; resubmitted clean at step 7). Both artifacts were self-corrected in one retry; neither recurred.

### F2. Spatial/geometry errors (every run; ~10 distinct incidents)

The dominant error class, and the one that separates the models.

- **Inserter-reach misunderstanding (Haiku, fatal).** Haiku repeatedly placed an input inserter adjacent to the ore chest with a furnace 3 tiles away, so the inserter dropped ore onto the ground (drop position `x=18.5` vs furnace at `x=20`; haiku, step 5 output). The environment said so explicitly — `'burner-inserter at (17.5, 72.5): output blocked by item on the ground. There is no sink entity in place to accept the output.'` (haiku, steps 4, 5, 7) — yet Haiku rebuilt the identical geometry four times (steps 2, 3, 5, 7) and ended the run with 113 ore buffered and **0 plates ever produced**.
- **Loop placement math (Sonnet, recoverable).** The same bug appears twice: a `for i in range(2)` drill-placement loop whose position arithmetic lands drill 1 on drill 0, throwing a collision exception (sonnet-B, step 1: "A stone-furnace already exists at {x=17, y=74}"; gear, step 7, same exception for the electric furnace). Related collisions: gear steps 12 and 14 (inserter placement blocked by second furnace; `connect_entities` from an occupied drop position).
- **Wrong-side rebuild (cable, caught).** Rebuilding the destroyed drill via `nearest_buildable`, Sonnet placed it *below* the furnace so its drop position (−46.5, 79.5) fed nothing (cable, step 10). It noticed by inspecting `drop_position` (step 11), verified the original tile was free with `can_place_entity` (step 12), and did pickup-and-replace at the exact original position (step 13).

Sonnet treats these as debuggable events — inspect, adjust, retry; Haiku treats them as mysteries (see F3).

### F3. Abandon-and-rebuild instead of debug (Haiku only, 2 incidents)

Haiku responded to persistent geometry failures with demolition: "=== Complete factory rebuild - simplified approach ===" (haiku, step 3) and then `print("=== RADICAL RESET: Starting completely fresh ===")` followed by picking up all 8 placed entities (haiku, step 6) — after which it rebuilt the same 3-tile inserter gap that caused the problem (step 7). The resets destroyed working state (a chest holding 183 buffered ore, step 3) without touching the actual defect. No Sonnet run ever did a full teardown; the calmest variant is sonnet-A settling into a fixed `refuel_if_low` monitoring loop for its last 14 steps (sonnet-A, steps 18–31).

### F4. Alarm fatigue: chronic-alert tolerance (all 4 Sonnet runs)

Every Sonnet factory ran its entire life with standing alerts the agent decided were benign — chiefly `output blocked by item on the ground` from drills gravity-feeding furnaces (sonnet-A, steps 7–31, every single step; sonnet-B, steps 15–29; cable, steps 14–30). The agents were arguably right that these were cosmetic, but the habit degrades the alert channel exactly where WRENCH needs it: the signature of a destroyed drill is a `no ingredients to smelt` alert appearing amid the same chronic noise (sonnet-A, step 2). Detection in practice therefore relied on entity queries, not alerts (F5).

### F5. Detection behavior: the NoneType stack trace is the real detector

In all three runs where Sonnet detected an entity destruction, the mechanism was identical and none of it was alert-driven:

1. A routine status sweep calls `get_entity(...)` on the destroyed entity, which returns `None`, and the agent's own f-string crashes: `AttributeError: 'NoneType' object has no attribute 'status'` (sonnet-A, step 2; cable, step 8; gear, step 21).
2. A `get_entities` area scan confirms the absence — or, in the gear run, finds the smoking gun: an `entity-ghost` at the assembler's coordinates in a neighbour list (gear, step 26).
3. `report_fault` plus rebuild (sonnet-A, step 5: "burner mining drill destroyed - no remnants"; gear, step 27: "assembling machine 2 destroyed - entity-ghost remains"; cable, step 10). Throughput corroborated but did not trigger detection (sonnet-A watched 22→19→6/60s across steps 1–4 before acting).

**The exception matters most: sonnet-B never detected its disruption at all.** The fire destroyed drill 1 at (19, 72) seconds after placement, mid-step (ledger tick 5653695, between step 0 at tick 5632107 and step 1's end at 5658884). The agent saw the resulting ghost and concluded "Something went wrong with drill placement math (2nd drill landed on 1st)" (sonnet-B, step 2), later "Remove the stray ghost from the failed second drill placement" (step 3) — misattributing the disruption to its own bug. It then rebuilt capacity elsewhere for throughput reasons (step 4). Its 10 fault reports (ledger ticks 5774330–6217137) are all endogenous fuel/jam faults ("burner-mining-drill out of fuel", "fuel-chest ran dry", "wooden-chest full"); none mentions destruction. Yet the meta shows recall 1.0 and precision 0.9 — a nearby fuel report loose-matched the fire. Detection credit here is an artifact of the loose radius (see V3), and the caller-facing summary "recovered, precision 0.9" is technically true but describes an agent that never knew anything had been destroyed.

Over- vs under-reporting spans the observed range: sonnet-B filed 10 reports (9 loose-matched → precision 0.9, rewarding verbosity), cable filed 2 (precision 0.5: the drill report matched, "boiler out of fuel - power generation at risk" did not; ledger ticks 150749, 215925), gear filed 3 (precision 0.67 loose but 0.33 strict — its report crediting the belt_cut was "furnace2 output inserter permanently blocked at belt merge point" at (20.5, 73.5), five tiles from the actual cut at x=15.5; gear, step 30 / ledger tick 9781403).

### F6. Recovery quality: repair-in-place wins the first disruption, nothing survived a second

- **Repair-in-place** (3/3 detected destructions): exact-position rebuild plus refuel — sonnet-A step 5 (throughput 6→37.5/60s by step 6, 2.4× the 16/60s quota); gear step 27 (gears resumed, 19/60s by step 28); cable step 13 after the F2 detour.
- **Rebuild-elsewhere, unknowingly**: sonnet-B's step-4 second line at (26, 74) restored throughput (TR capped at 1.5) without the agent knowing it was a recovery.
- **No redesign for robustness**: after repeated fuel outages, only sonnet-B built fuel-chest loops (steps 8, 12), and those were partly mis-oriented (`furnace at drop position is blocked` warnings persisted, steps 15–29) and still required manual coal top-ups.
- **The second disruption killed the gear run.** The belt_cut (ledger tick 9628153, three belts at x=15.5 severed ~2 minutes after the assembler kill, while the agent was still diagnosing the first fire) was never repaired. The agent saw blocked-belt alerts (gear, step 24), diagnosed a "permanently blocked belt merge point" (step 30), and spent its final two steps on wide `get_entities` dumps — the cut belt's `entity-ghost` at (15.5, 72.5) is visible in step 31's output, unremarked. Compounding endogenous failure: the boiler ran dry twice (steps 22–23 and again by step 30, when every electric machine reads `NO_POWER`). `samples.json` confirms the last gear was produced at tick 9780304; output was zero for the final ~20k ticks. The task failed.

### F7. Endogenous fragility competes with the injected disruption

Across all Sonnet runs, burner-fuel logistics — not the disruption — was the largest recurring threat: drills/furnaces/inserters starving on coal (sonnet-A, steps 8, 16; sonnet-B, steps 5, 17; cable, step 27; gear's boiler, twice). All 10 of sonnet-B's fault reports and 2 of gear's 3 concern endogenous faults. For a disruption benchmark this is signal-to-noise: the environment's natural failure rate generates report traffic and throughput dips that both mask and mimic injected faults.

### F8. Design-avoidance is real but accidental

Both iron-plate runs used a beltless gravity-feed design (drill drops directly into the furnace beneath: "Placed StoneFurnace 0 ... to catch ore from drill 0", sonnet-A, step 1), so belt_cut could not arm: ledger `failed`/`"no belts"` (sonnet-A tick 2797799; sonnet-B tick 5660911). The cable run built exactly one drill, which the first fire destroyed, making resource_exhaustion `not_applicable` `"no drills"` (cable, ledger tick 119638) — the sequential-contamination bug's second face (V2). The early-build reasoning shows no disruption-aware evasion: designs were chosen for simplicity and throughput redundancy ("2 parallel drill+furnace lines on the iron ore patch for redundancy", sonnet-B, step 1). Still, minimal builds shrink the disruption surface, so task generators must either guarantee each armed kind's precondition or score unarmable kinds explicitly.

### F9. Endogenous failure crowds out detection of the real disruption (supplementary run: gear2)

A sharper, single-incident version of F7, collected specifically to check how the agent behaves when a self-inflicted failure and an injected disruption collide in the same window — the V2 fix (recovery-chained arming) made this observable for the first time, since previously the second fire could land contaminated by the first before the agent had even noticed it.

gear2 built a full steam power chain (offshore pump → boiler → steam engine → electric poles → electric drills/furnaces/assembler; steps 6–17) but hand-fed the boiler once (step 7: `insert_item(Prototype.Coal, boiler, quantity=50)`) and never built automated coal resupply. `entity_destruction` fired on the assembler at tick 10745075; independently, the boiler ran dry on its own — an endogenous failure, not an injected one. Both surfaced together at step 26 (tick 10809369):

```python
report_fault(Position(x=25.5, y=85.5), "assembling-machine-2 missing/destroyed - was working, now gone from entity query")
report_fault(Position(x=-5.0, y=73.5), "boiler out of fuel - power network down, all downstream drills/furnaces/inserters lost power")
```

The assembler report is correct and matches the fire. But `belt_cut` had also fired (tick 10862417, three belts at x=19.5) and is never reported at all — the agent spent step 26 refueling the boiler instead. Final detection: recall 0.5 (1 of 2 fires reported), precision 1.0 on what it did report (gear2 meta). Both disruptions physically recovered (TR 0.26 and 1.5) — the failure is purely in situational awareness, not repair capability, and it is the inverse of F6's gear run: there, recovery failed and detection (partially) succeeded; here, recovery succeeded and detection partially failed. Together they suggest recovery and detection degrade somewhat independently under compounding load, which is the reason WRENCH scores them as separate axes (§3, V3) rather than folding them into one number.

### F10. Constant rebuild friction masks disruption under scarcity (scarcity-cal)

scarcity-cal is the cleanest recovery in the whole dataset by throughput — both disruptions fully recovered (TR 1.33 on `entity_destruction`, 1.19 on `resource_exhaustion`) — and also the first run with **zero `report_fault` calls across 48 steps**, despite two real fires. The evidence it noticed is explicit: step 8 extracts the surviving plates from a furnace, then `pickup_entity(furnace)` followed by `print("Picked up dead drill: True")` (scarcity-cal, step 8) — the agent identified the drill as dead, salvaged what it could, and relocated to a fresh patch at `(25, 82)` the very next step, never once narrating it as a fault to report.

Read against F6/F9's "rebuild-elsewhere, unknowingly" pattern, scarcity sharpens rather than introduces this failure mode: starting from nothing means the agent spends the whole run in a build → hit an error → adapt loop as a matter of course (three separate drill/furnace placements across steps 3–12 alone, each hitting its own friction — a `get_entities` keyword-argument `TypeError` at step 5, a bad `get_entity` call on the now-missing drill at step 7). Against that baseline, "the drill is dead" doesn't register as anomalous; it looks like the same category of routine setback as everything else in the run. A benchmark that wants detection to mean something under scarcity may need a signal that survives this — e.g. crediting detection implicitly when a `pickup_entity`/rebuild sequence targets a ledger-affected entity, not only explicit `report_fault` calls (a scoring change, not a task change).

### F11. Design-avoidance can leave a task with no teeth (adaptive-cal)

adaptive-cal built a textbook two-line burner-tier factory (drill → furnace, no belts, no electric anything — the same design pattern as sonnet-A/sonnet-B in F8) and cleared quota by step ~15. The engine did exactly what it should: `adaptive_strike` armed the moment quota was proven (tick 12698725) and immediately resolved `not_applicable` `"no electric poles"` in the same tick (ledger). This is the design-avoidance path (F8) working as specified, not a bug — the pilot script's summary JSON only counts `event == "fired"` toward `"fires"`, so it under-reports what happened; the full story is only visible in the raw ledger.

The gap this exposes was real: **`iron_plate_adaptive_sentinel` had exactly one disruption in its list, and burner-tier is both the simplest and the most common solution to a 16/60s iron-plate quota** (every iron-plate run so far except gear2's power chain used it). Unlike `iron_plate_sentinel`, which chains `entity_destruction` (kind-agnostic, always applies) ahead of `belt_cut` (design-avoidable), the adaptive sentinel had no fallback — a burner-tier build got zero disruption pressure for the entire episode. **Fixed** (commit `db3d4653`): `entity_destruction` now chains ahead of `adaptive_strike`, so the task always tests something regardless of build style. §2.11's grid data confirms this in the field: 2 of 3 models still built burner-tier and got a real, non-`not_applicable` disruption out of it.

### F12. Cross-model grid, all three new task families (§2.11 — N=1, unreplicated)

A 3×3 grid (sonnet/opus/fable × observability/adaptive/scarcity, one seed each) run specifically to see what breaks across capability tiers. Every one of the 9 episodes fully recovered (`recovered: true` on every fire) — none catastrophically failed like the original `iron_gear_sentinel` runs — so at this difficulty tier the three families discriminate on *how* models recover, not *whether*.

**Observability gives the cleanest signal of the grid**, and it's the one result here worth trusting even at N=1 because it's a monotonic three-point ordering, not a single outlier: Opus used 19/48 calls (40% utilization, TR 1.5 capped), Sonnet 29/48 (60%, TR 0.91 — the only sub-1.0 result in the whole observability column), Fable 38/48 (79%, TR 1.5 capped, worst strict-precision at 0.45). Budget efficiency and recovery quality didn't trade off against each other here; Opus got both.

**Adaptive confirmed the F11 fix end-to-end against a real model, not just scripted tests**: Opus was the only one of the three to build electric infrastructure for a plain iron-plate quota, which is exactly the precondition `adaptive_strike` needs — it fired for real (2 fires total: `entity_destruction` at tick 15530964, `adaptive_strike` at tick 15569914, both recovered, TR 0.78/0.85). Sonnet and Fable both built burner-tier and got only the `entity_destruction` fallback, `adaptive_strike` resolving `not_applicable` as designed.

**Scarcity produced the grid's one surprising result, and it needs a caveat before it means anything**: Opus scored the only sub-1.0 TR (0.56 / 0.57) and the only recall-0.0 in the whole grid, on the model that is otherwise the strongest performer here (best observability efficiency, only model to trigger `adaptive_strike`). Reading the trajectory rules out a capability failure: at step 44, after `resource_exhaustion` depleted its patch, Opus systematically probed four candidate relocation sites (`y=80/82/84/86`, checking patch size at each before committing) rather than taking the first option Sonnet/Fable grabbed; by step 47/48 it was still placing and fueling the new drill when `verify()` ran. TR 0.56 is "recovery caught mid-flight by the step budget," not "recovery failed" — and recall 0.0 is consistent with F10 (constant rebuild friction crowding out `report_fault`), now observed on a second model. **This reads as a thoroughness-vs-speed tradeoff that a fixed step budget converts into a bad score, not a genuine capability gap — but it is one data point.** A same-seed-family replication (`--seed-offset 100`, same task/model, launched immediately after this finding) is the direct test: if TR stays low, it's a real Opus/scarcity interaction; if it clears 1.0, step 47/48 was just an unlucky cutoff.

## 3. Benchmark-validity observations

Each meta-finding below was caught because a pilot run exhibited it; fix status as of this writing.

- **V1. Mid-ramp arming inflates TR** (fixed: full-quota arming). Baselines were sampled while factories were still ramping: cable baseline 20.2/60s vs steady-state ~38 (cable meta; step-7 throughput print already 142 cable-equivalents); sonnet-B baseline 9.08 vs steady ~18–19. Post-recovery throughput trivially exceeds these baselines, so TR pegged the 1.5 cap in both runs. Arming now waits for full quota. (Related, separately fixed: sonnet-A's baseline/TR are `null` from a sampling bug, so its "recovered 2.4× quota" rests on trajectory throughput prints, not the metric.)
- **V2. Sequential-fire contamination** (fixed: recovery-chained arming). The second disruption armed on a fixed schedule regardless of recovery state. Gear: belt_cut fired 7,216 ticks after the assembler kill, before the agent had even noticed the first fire, so its baseline was 0.0 and TR null (gear meta, fire_1). Cable: resource_exhaustion armed the same tick the first fire destroyed the run's only drill → `not_applicable "no drills"`. Arming is now chained on recovery from the previous fire.
- **V3. Loose-radius detection credit** (fixed: `precision_strict` added). Sonnet-B earned recall 1.0 / precision 0.9 without ever diagnosing the destruction (F5); gear's belt_cut "detection" was a report about a different, real fault five tiles away (precision 0.67 loose vs 0.33 strict). Strict-radius matching now reported alongside loose.
- **V4. TR cap saturation on scaling factories** (documented limitation). Any agent that expands capacity post-fire saturates TR at 1.5 (sonnet-B and cable both hit exactly 1.5), destroying resolution among good recoveries. Even with full-quota baselines this persists whenever agents overbuild; treat TR = cap as "recovered, magnitude uninformative."

## 4. What differentiates models at pilot scale

With N=5 nothing here is more than directional. The Haiku/Sonnet gap in this pilot is a capability floor, not a recovery gap: Haiku never produced a single plate in 8 steps (its second attempt; the first crashed pre-step-0), so no disruption ever armed and the sentinel machinery was never exercised — its meta precision/recall of 1.0 are vacuous (haiku meta, fires: 0). All four Sonnet runs built past quota and scored recall 1.0 on every fire, but with important variance underneath: one detection was genuine three times (via the NoneType-crash → area-scan → ghost pattern) and spurious once (sonnet-B, V3); precision ranged 0.5–0.9 loose and dropped to 0.33 strict in the gear run; and the only run that faced a second disruption died to it, with the belt ghost literally on screen at the final step. If this pattern holds at scale, the discriminating measurements are strict precision and second-disruption survival, not first-fire recall.

The gear2 supplementary run (F9) sharpens this further: with sequential-fire contamination fixed, Sonnet recovered from *both* disruptions physically (TR 0.26 and 1.5) but only detected one (recall 0.5) — the opposite failure shape from the original gear run, where recovery failed but detection was intact. Two data points is not a trend, but it is consistent with recovery and detection being separable capabilities rather than one competence measured twice, which is the working assumption behind scoring them on independent axes.
