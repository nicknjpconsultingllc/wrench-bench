# FLE observation-fidelity audit

Audit of the Factorio Learning Environment's environment layer against a live
Factorio **2.0.73** headless server (RCON `localhost:27000`), performed on this
fork. Upstream issue numbers refer to
`github.com/JackHopkins/factorio-learning-environment/issues`.

Every repro script is standalone:

```bash
source .venv/bin/activate
python audit/repro_378_stale_alerts.py         # etc.
```

Convention: **PASS = bug reproduced against the current code**, FAIL = not
reproduced (either fixed here, or the claim did not hold — the evidence lines
say which). After the fixes in this fork, the repros for #378, #379 and the
production-stats suspicion print FAIL by design.

## Summary

| Bug | Claim | Reproduced? | Fixed here? | Repro script |
|---|---|---|---|---|
| #378 | `get_alerts` returns only alerts *older* than the window, deletes on read, never invalidates repaired issues | **Yes** — all three behaviors confirmed | **Yes (FIX A)** | `repro_378_stale_alerts.py` |
| #379 | `get_entities` blind to neutral-force ground items that block placement | **Yes** — items invisible; `can_place_entity` returns False on their tile (while `place_entity` still succeeds — an extra inconsistency) | **Yes (FIX B)** | `repro_379_ground_items.py` |
| #377 | drill `drop_position` wrong / unusable | **Yes** — reported (15.5, 69.5) vs engine (15.5, 69.703125); furnace at reported point fails with "something is in the way" | No (audit only) | `repro_377_drill_drop_position.py` |
| NEW-4 | production stats swap `input_counts`↔consumed | **No** — labels are correct end-to-end; the Lua contained TWO inversions that cancel out | **Clarity fix (FIX C)** + regression tests | `repro_prodstats_label_swap.py` |
| NEW-5 | silent character respawn with inventory wipe | **Yes** — death, teleport to origin and full inventory loss are all invisible in the next observation | No (audit only) | `repro_silent_respawn.py` |
| #375 | pathfinding treats belts as obstacles | **Latent, not agent-visible** — the collision mask *does* list `transport_belt` (raw `request_path` without `allow_paths_through_own_entities` → `not_found` through a belt ring), but `move_to` always sets that flag, so own-force belts don't block agents in this fork | No (audit only) | `repro_375_376_380_pathing.py` |
| #376 | `request_path` silent permanent failure from inside a collision box | **No** — character escaped both an own-force furnace and a neutral-force huge-rock spawned on top of it | No | `repro_375_376_380_pathing.py` |
| #380 | off-grid placement misreports "something is in the way" | **No** — in an empty area, `place_entity(stone-furnace at (30.5, 30.5))` snaps to (31, 31) and succeeds; `can_place_entity` agrees | No | `repro_375_376_380_pathing.py` |

## Details per bug

### #378 — alerts stale + destructive reads (REPRODUCED, FIXED)

`fle/env/mods/alerts.lua`. Old `storage.get_alerts(seconds)`:

```lua
if current_tick - alert.tick > 60*seconds then   -- OLDER than window
    table.insert(old_alerts, alert)
    storage.alerts[key] = nil                    -- destructive read
end
```

and `on_tick` only ever *added* alerts (`if not storage.alerts[entity_key]`),
never removed them when the issue was repaired.

Evidence (before fix), furnace with ore but no fuel:

- `get_warnings(10)` ~3 s after the issue arose → `[]` (fresh alert invisible).
- `get_warnings(2)` after ~7 s → `['stone-furnace at (0, 2): out of fuel']`;
  the *immediately following identical call* → `[]` (read consumed the alert,
  furnace still broken).
- After inserting coal (furnace `WORKING`, entity `warnings=[]`),
  `get_warnings(2)` still returned `'out of fuel'` (stale, never invalidated).

**FIX A** (same file, API shape unchanged — `get_warnings(seconds)` still
works): the scanner now *refreshes* existing alerts each 60-tick scan
(last-seen `tick`, keeping `first_tick`), drops alerts not refreshed by a
scan, and `get_alerts(seconds)` returns alerts seen **within** the last
`seconds` seconds, does **not** delete on read, and re-validates each alert
against the live entity on read (invalid entity or zero remaining issues →
alert dropped immediately). Returned records are plain-data copies (no
LuaEntity leaks into the serialized response).

Tests: `tests/audit/test_fix_a_alerts.py` (4 tests; the first three fail on
the old code by construction).

Side-finding (not fixed): `FactorioInstance.get_warnings` renders positions as
`tuple(alert["position"].values())`, which came out `(y, x)` — the furnace at
x=2, y=0 is reported as "at (0, 2)".

### #379 — get_entities blind to ground items (REPRODUCED, FIXED)

`fle/env/tools/agent/get_entities/server.lua` queried
`find_entities_filtered{area=..., force=player.force}`. Items dropped on the
ground are `item-on-ground` entities (type `item-entity`) on the **neutral**
force, so they were invisible. Evidence (before fix): 3 iron-plate stacks on
the ground at ~(3, 3); `get_entities(position=(3,3), radius=5)` → `[]`, while
`can_place_entity(stone-furnace at (3,3))` → `False`. (Curiously
`place_entity` at the same spot *succeeds* — the placement path and the
check path disagree about ground items; documented, not changed.)

**FIX B**:

- Server (`get_entities/server.lua`): additionally queries
  `type = "item-entity"` in the area and appends a **minimal plain-data
  representation** — `name="item-on-ground"`, `position`, `item` (stack name),
  `count`, `force="neutral"`. Full `serialize_entity` was deliberately not
  used: it assumes machine-like entities (health, status, inventories,
  `get_issues`) and its output could not be parsed into the Python `Entity`
  model (which requires `energy`, `health`, `dimensions`, ...) anyway.
  Ground items are only included when the caller passed no prototype filter,
  or explicitly requested `Prototype.ItemOnGround`.
- Python: new `ItemOnGround(EntityCore)` model
  (`fle/env/entities.py`) with `item`, `count`, `force` fields and a compact
  repr, plus `Prototype.ItemOnGround = "item-on-ground", ent.ItemOnGround`
  (`fle/env/game_types.py`). The existing `get_entities` client then resolves
  the prototype through its normal path — no client-side special-casing.

After the fix: `get_entities` returns e.g.
`ItemOnGround(item='iron-plate', count=7, position=Position(x=3.19921875 y=3.19921875))`.

Tests: `tests/audit/test_fix_b_ground_items.py` (4 tests; visibility tests
fail on the old code).

### #377 — drill drop_position (REPRODUCED, not fixed)

`fle/env/mods/serialize.lua` rounds the engine's `drop_position` to the
nearest 0.5 (`math.round(v * 2) / 2`). Measured: drill placed at (16, 71)
facing UP; engine drop_position **(15.5, 69.703125)**, Python entity reports
**(15.5, 69.5)** (Δy = −0.203). The canonical use — placing a stone-furnace at
the reported drop position — fails:
`"Cannot place stone-furnace at x=15.5 y=69.5 - something is in the way or terrain is unplaceable"`
(exactly the observation in the issue). Note the deeper problem: a 2×2
furnace *centered* on the drop point always overlaps the drill; a usable
"furnace under the drill" position must cover the drop **tile** (here center
(16, 69)), so even an exact drop_position is a trap for agents without
snapping guidance.

### NEW-4 — production statistics label swap (NOT a bug end-to-end; clarity FIX C)

Suspicion: `fle/env/tools/admin/get_production_stats/server.lua` maps
`input_counts` → consumption and `output_counts` → production, inverted with
respect to Factorio's semantics.

Empirical ground truth (raw RCON, live 2.0.73 server, after smelting 4
plates from 5 ore): `input_counts["iron-plate"] +4` (input = **produced**),
`output_counts["iron-ore"] +5` (output = **consumed**) — Factorio semantics
confirmed.

What the audit actually found: the old Lua contained **two** inversions that
cancel out. The locals were misnamed (`input_counts` stored into
`consumption_diff`, `output_counts` into `production_diff`) **and** the
return table swapped them back (`output = consumption_diff`,
`input = production_diff`). Net effect at the Python boundary — verified
empirically: FLE `output["iron-plate"] +4`, `input["iron-ore"] +5`, i.e.
**"output" = produced, "input" = consumed**, exactly the convention every
downstream consumer uses. No observable bug; accusing upstream of shipping
swapped stats would be wrong.

**FIX C**: rewrote the function as a straight, correctly-named mapping
(`item_produced_counts = item_stats.input_counts` → `production_diff` →
returned as `output`, and mirror for consumption; identical treatment for
fluids), with a comment documenting the verified engine semantics. External
behavior is intentionally unchanged (verified by re-running the repro before
and after: identical deltas).

Downstream consumer audit (all read "output" as *produced*, "input" as
*consumed* — consistent, so **no consumer changes were needed**):

- `fle/commons/models/achievements.py` — `ProductionFlows.input/.output` are
  plain carriers; `get_new_flows` diff-only.
- `fle/env/utils/profits.py` — dynamic profit adds value of `output`,
  subtracts `input`; static pass removes crafted/harvested from `output`.
- `fle/env/utils/achievements.py` — achievements credit `post.output` as
  created items.
- `fle/env/gym_env/observation.py` / `observation_formatter.py` — formats
  `input` as "consumed", `output` as "produced" (docstrings say so
  explicitly).
- `fle/env/gym_env/environment.py`, `fle/eval/algorithms/mcts/*`,
  `fle/eval/inspect/integration/solver*.py` (produced-item extraction from
  `flow["output"]`), throughput tasks (via achievements) — all consistent.

Tests: `tests/audit/test_fix_c_production_stats.py`. Honest caveat: because
old and new behavior are identical, these tests pass on both; their job is to
**lock** the end-to-end semantics so that a future *single* inversion (the
bug this audit went looking for) fails loudly.

### NEW-5 — silent respawn with inventory wipe (REPRODUCED, not fixed)

`fle/env/mods/utils.lua` `storage.utils.ensure_valid_character` (called at
the top of essentially every tool) recreates a dead character at spawn
(`{0, (player_index-1)*2}`) with an empty inventory. Evidence: character at
(10.5, 10.5) carrying `coal=3, iron-plate=5`; killed via `.die()`; the next
ordinary tool call returned observation `'1: (Inventory(),)'` — a perfectly
normal-looking result with no mention of death/respawn — and the character
was back at (0, 0) with nothing. An agent has no way to distinguish this
from "my inventory was always empty". A fix would surface a death event in
the next observation (and arguably preserve/recover the corpse inventory);
out of scope for this audit's three fixes.

### #375 / #376 / #380 — pathfinding & collision messages (audit only)

- **#375**: the claim is true of the collision mask but masked in practice.
  `fle/env/tools/admin/request_path/server.lua` includes
  `collision_mask.layers.transport_belt = true` (characters can actually walk
  over belts). A raw `request_path(..., allow_paths_through_own_entities=false)`
  from (0,0) to the center of a closed belt ring returns `not_found` —
  belts ARE hard obstacles to the mask. However `move_to` always passes
  `allow_paths_through_own_entities=True`, and agent-built belts are always
  own-force, so `move_to((12,0))` into the ring succeeded (final position
  (12.5, 0.5)). Verdict: latent defect, **not reproducible through the agent
  API** in this fork. Belts owned by another force would still block.
- **#376**: not reproduced. With a stone-furnace script-spawned exactly on
  the character, `move_to` escaped and completed (own-force case is covered
  by the flag above); with a neutral-force `huge-rock` created on the
  character, the engine pathfinder still recovered and `move_to` completed.
  Side-finding while probing this: `move_to` routinely stops up to ~2 tiles
  short of the requested goal (request_path nudges the goal via
  `avoid_entity` / `find_non_colliding_position`); an early version of this
  repro with a 1.5-tile tolerance flagged that slop as a flaky "failure".
  The repro now checks specifically for the issue's failure mode (permanent
  no-movement/path error) and is stable across repeated runs.
- **#380**: not reproduced. In a verified-empty area, placing a 2×2
  stone-furnace at the off-grid center (30.5, 30.5) *snaps* to (31, 31) and
  succeeds; `can_place_entity` at the off-grid point returns True. The
  misleading "something is in the way" message does still appear where there
  is a *real* post-snap collision (see #377), but we could not produce a case
  where grid alignment alone was misreported as an obstruction.

## Fixes: files touched

| File | Fix | Change |
|---|---|---|
| `fle/env/mods/alerts.lua` | A | `on_tick` refreshes/expires alerts; `get_alerts` non-destructive, within-window, re-validates on read |
| `fle/env/tools/agent/get_entities/server.lua` | B | append minimal neutral-force `item-on-ground` records (honors prototype filter) |
| `fle/env/entities.py` | B | new `ItemOnGround(EntityCore)` model |
| `fle/env/game_types.py` | B | new `Prototype.ItemOnGround` entry |
| `fle/env/tools/admin/get_production_stats/server.lua` | C | remove the self-cancelling double inversion; behavior-preserving, documented mapping |

New (non-fix) files: `audit/*` (this directory), `tests/audit/{conftest.py,
test_fix_a_alerts.py, test_fix_b_ground_items.py, test_fix_c_production_stats.py}`.

## Verification

- All five repro scripts re-run after the fixes: #378, #379 and NEW-4 now
  print FAIL (not reproduced / behavior correct); #377 and NEW-5 still PASS
  (reproduced, deliberately unfixed); #375/#376/#380 unchanged (audit-only).
- `pytest tests/audit/` — 10 passed.
- Targeted upstream suites: `tests/test_warnings.py`,
  `tests/test_production_stats.py` (4 passed), `tests/actions/test_get_entities.py`
  (20 passed), `tests/actions/test_get_entity.py` + `tests/status` (14 passed),
  `tests/eval/test_achievements.py` + `tests/gym_env/test_observation_formatter.py`
  (passed).
- Pre-existing failures unrelated to this audit (verified against untouched
  files): `tests/eval/test_profits.py::test_profits` fails with
  `AttributeError: 'FactorioInstance' object has no attribute 'get_production_stats'`
  (`fle/env/utils/profits.py:2` calls an instance-level method that does not
  exist in this fork — Python-level failure before any RCON call). One
  `test_get_entities` flake was caused by a leftover character corpse from the
  NEW-5 repro (world state, not code); it passes after clearing corpses.
- Environment note: `pytest-xdist` was missing from the venv (the shared
  `tests/conftest.py` `instance` fixture requires its `worker_id` fixture);
  installed via `uv pip install pytest-xdist` to run the upstream suites.
