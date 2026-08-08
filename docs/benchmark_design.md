# WRENCH benchmark design: from portfolio artifact to real benchmark

Distilled from a survey of benchmark-construction practice as of Aug 2026
(sources at bottom). Drives the roadmap from the current 3-sentinel pilot to a
leaderboard-grade benchmark.

## The core reframe

"10+ scenarios combined into a score" is the wrong model on all three counts.
Respected agentic benchmarks span **1 scenario** (Vending-Bench, GBA Eval —
both cited by frontier labs) to 2,294 (SWE-bench Full). Task count is not the
gate. The N=1 benchmarks survive on five properties:

1. **Continuous, wide-range metric** (dollars; % frames correct) — far more
   statistical power per run than binary pass/fail. WRENCH has this
   (Throughput Retained).
2. **Human/oracle anchor** ("a good human makes ~$63K/yr; models make $5.5K").
   WRENCH lacks this — fix is cheap (oracle repair scripts are needed anyway).
3. **Long horizon** — the effective sample is the thousands of decision points
   per trajectory, not the task list. WRENCH has this.
4. **Named failure modes with narrative force** ("meltdown loops"). WRENCH has
   the beginnings (crash-mediated detection, alarm fatigue, sees-but-can't-hold).
5. **Headroom** — nobody near ceiling. WRENCH has this (Sonnet fails gear).

**The actual binding gap is model count, not scenario count**: with 2 models
you cannot demonstrate resolving power. Real-benchmark bar: ≥6 models spanning
a capability range, with adjacent-model CIs separating.

## Scenario strategy: factorize, don't enumerate

Replace hand-authored sentinels with a **factorial grid**:
`base factory (4–5, varying chain depth/redundancy/buffering) ×
disruption family (5–6) × severity tier (3, a priori: ~20/50/85% of capacity
removed)` → 60–90 cells; ship **WRENCH-Lite** (~15–20 cells, the cheap entry
point à la SWE-bench Lite) and **WRENCH-Full**. Floor for bootstrap-over-cells
statistics: **≥30 cells**.

Missing disruption families to add:
- **Silent throughput throttle** (no visible entity change) — the real
  detection test; current families only measure "did you notice the explosion."
- **Power brownout** (degradation, not destruction).
- Stretch: compound/cascading (two families, chained on recovery — engine
  already supports this).

## Seeds, repeats, statistics

- **k=5 seeds per cell** (field standard: Vending-Bench 5, Terminal-Bench ≥5).
- **Same seed set across all models** (paired design / common random numbers) —
  the highest-leverage free variance reduction; almost nobody does it.
- **Cluster bootstrap over cells** for the headline 95% CI (runs within a cell
  are correlated; naive SEs can be ~3× optimistic — Miller 2024).
- **Run an ICC pilot first** (3 cells, k=10–16, one model): if within-cell
  variance is small, spend budget on more cells with k=3; if meltdown-like
  (likely for recovery tasks), k=5 is the floor. Publish the ICC — almost no
  benchmark does; it justifies the design.

## Scoring strategy

**One headline number, homogeneous family only:**
> **Throughput Retained (macro)** — mean across cells of the post-disruption
> production integral ÷ frozen-baseline counterfactual, bounded [0,1],
> cluster-bootstrap 95% CI.

Changes from current implementation: **macro (mean of per-cell ratios), not
pooled** — pooled overweights high-throughput cells (Arrow-style outlier
domination) and breaks bootstrap-over-cells. Report pooled as secondary.

**Companion axes, reported beside, never folded in:**
- **Recovery@budget** and **recovery^k** (all k seeds recovered — the
  worst-case-reliability framing, right for a resilience benchmark).
- **Detection profile**: precision(strict)/recall + **survival-analysis
  latency** (Kaplan–Meier with censoring at the tick budget; restricted mean
  time-to-detection). Non-detections are right-censored — a mean over
  detected-only runs is biased toward models that only catch easy faults.
- Detection and recovery are different constructs; merging invites gaming
  and Arrow problems. HELM gives cover for refusing a composite; SWE-bench
  gives cover for the single homogeneous headline. Do both.

**Anchors:** publish oracle-script Throughput Retained per cell as the
ceiling (+ a human number if obtainable).

## Contamination & gaming

- Publish the disruption **generator**; keep a **private, quarterly-refreshed
  seed pool** for leaderboard runs (versioned, `WRENCH-2026Q4`).
- Honest limitation: seeds resist *instance* memorization, not Factorio
  *domain* knowledge (huge public corpus incl. FLE trajectories).
- **Adversarial cheat trials before publishing**: trivial parallel lines,
  buffer-draining inflating early post-fire windows, sensor gaming. This is
  the most likely public-dunk vector for a throughput-ratio metric.
- Oracle solvability check per cell; success criteria must accept all valid
  recovery styles (repair-in-place vs rebuild-elsewhere).

## The adoption bar (reconstructed — no lab publishes one)

Ordered by leverage: (1) ≥6 models with monotone resolving power; (2)
one-command containerized harness (Terminal-Bench+Harbor is the proof this
outweighs task count: 101 agents in 4 months); (3) oracle solutions + cheat
trials; (4) arXiv paper (hard prerequisite for the inspect_evals register
flow since May 2026); (5) inspect_evals registration → then Epoch AI hub and
Artificial Analysis mirroring; plus published cost-per-run, full trajectory
logs, k=5 submission requirements, ABC-checklist compliance appendix.

## Sequenced next steps for WRENCH

1. ICC pilot (3 cells × k≈12, one model) → locks k and the breadth/depth split.
2. Silent-throttle + brownout families; severity knob; factory configs → grid.
3. Macro-TR headline + survival-analysis detection latency in scoring.
4. Oracle repair scripts (double as solvability validation + ceiling anchor).
5. Cheat trials.
6. 6-model matrix on WRENCH-Lite via the Inspect runner.
7. arXiv writeup → inspect_evals `/register/` → Epoch/AA outreach.

## Key sources

Miller 2024 (arXiv 2411.00640) · Anthropic statistical-approach post · ICC for
agentic evals (2512.06710) · Efficient Benchmarking (2603.23749) · SOTA
Evidence position (2605.17273) · ABC checklist (2507.02825) · Beyond Arrow
(2602.07593) · HELM (2211.09110) · BenchJack (2605.12673) · Terminal-Bench
(2601.11868) · τ-bench (2406.12045) · Vending-Bench (2502.15840) · GBA Eval
(mechanize.work) · LiveBench/LiveCodeBench · inspect_evals methodology.md ·
Epoch AI benchmarks/about.
