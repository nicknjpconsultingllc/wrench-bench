# WRENCH comparison table

- models: openrouter/anthropic/claude-sonnet-5, openrouter/openai/gpt-5.1, openrouter/google/gemini-2.5-pro
- tasks: iron_plate_observability_sentinel, iron_plate_adaptive_sentinel, iron_plate_scarcity_sentinel
- seeds per (model, task): 2
- generated: 2026-08-10 17:15:10

Aggregates use the pooled-ratio rule: sum of raw numerators over sum
of raw denominators across seeds/fires (never mean-of-ratios).
`-` = not scoreable (no fires with a valid frozen baseline).
TR is winsorized to [-0.5, 1.5] (see fle/disruptions/scoring.py) and
is the headline metric. TR (raw) is the same pooled ratio before
that clamp -- it can exceed 1.5 when recovery dramatically
overbuilds past the pre-disruption baseline, which TR alone cannot
show once it pegs at the cap (docs/failure_taxonomy.md finding V4).
TR (floor-adj) subtracts a passive-redundancy floor (fixed at fire
time, non-manipulable -- see
fle/disruptions/scoring.py:floor_adjusted_throughput_retained_parts)
from both TR's numerator and denominator, isolating the agent's own
recovery contribution. Only defined for entity_destruction fires; `-`
elsewhere (e.g. no entity_destruction fire this episode/group).

## Per-(model, task) aggregates

| Model | Task | Episodes | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | Det. recall | Det. precision (strict, 3-tile) | Det. precision (loose, 10-tile) | Det. latency (ticks) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openrouter/anthropic/claude-sonnet-5 | iron_plate_adaptive_sentinel | 2/2 | 2 | 1.500 | 2.091 | 1.500 | 1.00 | 0.50 | 1.00 | 1.00 | 11449 |
| openrouter/anthropic/claude-sonnet-5 | iron_plate_observability_sentinel | 2/2 | 2 | 1.500 | 2.645 | 1.500 | 1.00 | 0.00 | 1.00 | 1.00 | - |
| openrouter/anthropic/claude-sonnet-5 | iron_plate_scarcity_sentinel | 2/2 | 4 | 1.477 | 1.477 | 1.500 | 1.00 | 0.50 | 0.67 | 0.67 | 138778 |
| openrouter/google/gemini-2.5-pro | iron_plate_adaptive_sentinel | 2/2 | 2 | 1.050 | 1.050 | 1.050 | 1.00 | 1.00 | 0.06 | 0.11 | 20676 |
| openrouter/google/gemini-2.5-pro | iron_plate_observability_sentinel | 2/2 | 3 | 0.864 | 0.864 | 0.961 | 1.00 | 0.67 | 0.11 | 0.11 | 41095 |
| openrouter/google/gemini-2.5-pro | iron_plate_scarcity_sentinel | 2/2 | 4 | 0.527 | 0.527 | -0.156 | 1.00 | 0.75 | 0.25 | 0.25 | 14552 |
| openrouter/openai/gpt-5.1 | iron_plate_adaptive_sentinel | 2/2 | 0 | - | - | - | - | 1.00 | 0.00 | 0.00 | - |
| openrouter/openai/gpt-5.1 | iron_plate_observability_sentinel | 2/2 | 1 | 0.893 | 0.893 | 0.893 | 1.00 | 1.00 | 1.00 | 1.00 | 86115 |
| openrouter/openai/gpt-5.1 | iron_plate_scarcity_sentinel | 2/2 | 1 | 0.011 | 0.011 | -0.500 | 1.00 | 0.00 | 1.00 | 1.00 | - |

## Per-episode results

| Model | Task | Seed | Status | Fires | TR | TR (raw) | TR (floor-adj) | Recovery | Det. recall |
|---|---|---|---|---|---|---|---|---|---|
| openrouter/anthropic/claude-sonnet-5 | iron_plate_observability_sentinel | 0 | success | 1 | 1.500 | 2.131 | 1.500 | 1.00 | 0.00 |
| openrouter/anthropic/claude-sonnet-5 | iron_plate_observability_sentinel | 1 | success | 1 | 1.500 | 3.078 | 1.500 | 1.00 | 0.00 |
| openrouter/anthropic/claude-sonnet-5 | iron_plate_adaptive_sentinel | 0 | success | 1 | 1.500 | 2.087 | 1.500 | 1.00 | 1.00 |
| openrouter/anthropic/claude-sonnet-5 | iron_plate_adaptive_sentinel | 1 | success | 1 | 1.500 | 2.095 | 1.500 | 1.00 | 0.00 |
| openrouter/anthropic/claude-sonnet-5 | iron_plate_scarcity_sentinel | 0 | success | 2 | 1.438 | 1.438 | 1.500 | 1.00 | 0.00 |
| openrouter/anthropic/claude-sonnet-5 | iron_plate_scarcity_sentinel | 1 | success | 2 | 1.500 | 1.514 | 1.500 | 1.00 | 1.00 |
| openrouter/openai/gpt-5.1 | iron_plate_observability_sentinel | 0 | success | 0 | - | - | - | - | 1.00 |
| openrouter/openai/gpt-5.1 | iron_plate_observability_sentinel | 1 | success | 1 | 0.893 | 0.893 | 0.893 | 1.00 | 1.00 |
| openrouter/openai/gpt-5.1 | iron_plate_adaptive_sentinel | 0 | success | 0 | - | - | - | - | 1.00 |
| openrouter/openai/gpt-5.1 | iron_plate_adaptive_sentinel | 1 | success | 0 | - | - | - | - | 1.00 |
| openrouter/openai/gpt-5.1 | iron_plate_scarcity_sentinel | 0 | success | 1 | 0.011 | 0.011 | -0.500 | 1.00 | 0.00 |
| openrouter/openai/gpt-5.1 | iron_plate_scarcity_sentinel | 1 | success | 0 | - | - | - | - | 1.00 |
| openrouter/google/gemini-2.5-pro | iron_plate_observability_sentinel | 0 | success | 2 | 0.504 | 0.504 | 0.445 | 1.00 | 0.00 |
| openrouter/google/gemini-2.5-pro | iron_plate_observability_sentinel | 1 | success | 1 | 1.444 | 1.444 | 1.444 | 1.00 | 0.00 |
| openrouter/google/gemini-2.5-pro | iron_plate_adaptive_sentinel | 0 | success | 1 | 1.075 | 1.075 | 1.075 | 1.00 | 1.00 |
| openrouter/google/gemini-2.5-pro | iron_plate_adaptive_sentinel | 1 | success | 1 | 1.035 | 1.035 | 1.035 | 1.00 | 0.00 |
| openrouter/google/gemini-2.5-pro | iron_plate_scarcity_sentinel | 0 | success | 2 | 0.804 | 0.804 | 0.396 | 1.00 | 0.00 |
| openrouter/google/gemini-2.5-pro | iron_plate_scarcity_sentinel | 1 | success | 2 | 0.088 | 0.088 | -0.500 | 1.00 | 0.50 |
