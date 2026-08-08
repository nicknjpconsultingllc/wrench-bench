"""Audit-only repros for upstream issues #375, #376, #380 (pathfinding &
collision messages).  No fixes are implemented for these; this script
documents what actually happens on a live server.

#375 - pathfinding treats transport belts as hard obstacles.
    Code evidence: fle/env/tools/admin/request_path/server.lua builds the
    path request with collision_mask.layers.transport_belt = true, although
    a Factorio character can walk over belts.  Repro: enclose a target in a
    ring of transport belts and call move_to(target).

#376 - request_path fails when the character starts inside a collision box.
    Repro: script-spawn a stone-furnace directly on top of the character
    (script placement ignores collisions), then call move_to.

#380 - off-grid placement failures report "something is in the way" even
    in a completely EMPTY area, instead of explaining grid alignment.
    Repro: place_entity(stone-furnace) at (30.5, 30.5) far from anything.
    A 2x2 entity can never sit at a half-integer center on the tile grid,
    but the error blames a non-existent obstruction.

Each sub-repro prints its own PASS/FAIL.
"""

import os
import sys

os.environ.setdefault("FLE_GETPATH_MAX_ATTEMPTS", "12")  # don't hang on #376

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, lua, Reporter


def repro_375(inst):
    r = Reporter("#375", "pathfinding treats belts as obstacles")
    ns = inst.namespace
    from fle.env.game_types import Prototype
    from fle.env.entities import Position
    from fle.env import Direction

    # Build a closed ring of belts around (12, 0): perimeter of the square
    # x,y in [9.5, 14.5] -> interior tiles are free, ring is 1 belt thick.
    cmds = []
    for i in range(-3, 4):
        for pos in ((12 + i, -3), (12 + i, 3), (9, i), (15, i)):
            cmds.append(
                f'game.surfaces[1].create_entity{{name="transport-belt", '
                f"position={{x={pos[0]},y={pos[1]}}}, force=game.forces.player}}"
            )
    lua(inst, " ".join(cmds))
    n_belts = lua(
        inst,
        'rcon.print(#game.surfaces[1].find_entities_filtered{name="transport-belt"})',
    )
    r.note(f"built belt ring around (12,0); belts on surface: {n_belts}")
    r.note(
        "code evidence: request_path/server.lua collision_mask.layers includes "
        "transport_belt=true (characters can actually walk over belts)"
    )

    target = Position(x=12, y=0)
    error = None
    final = None
    try:
        final = ns.move_to(target)
        r.note(f"move_to((12,0)) returned final position {final}")
    except Exception as e:
        error = str(e)
        r.note(f"move_to((12,0)) raised: {error}")

    char_pos = lua(
        inst,
        "local c = storage.agent_characters[1] rcon.print(c.position.x..','..c.position.y)",
    )
    r.note(f"character position on server afterwards: ({char_pos})")
    cx, cy = (float(v) for v in char_pos.split(","))
    reached = abs(cx - 12) <= 1.5 and abs(cy - 0) <= 1.5

    # Isolate the collision mask itself: issue the same server-side
    # request_path but WITHOUT allow_paths_through_own_entities (the flag
    # move_to always sets, which lets the pathfinder ignore own-force belts).
    import time

    # Start OUTSIDE the ring at (0,0), goal inside at (12,0).
    rid = lua(
        inst,
        "rcon.print(storage.actions.request_path(1, 0, 0, 12, 0, 0, false, 1))",
    )
    status = None
    for _ in range(20):
        time.sleep(0.25)
        status = lua(inst, f"rcon.print(storage.actions.get_path({rid}))")
        if '"pending"' not in status and '"busy"' not in status:
            break
    r.note(
        f"raw request_path to (12,0) with allow_paths_through_own_entities=false -> {status}"
    )
    mask_blocks = status is not None and "not_found" in status

    reproduced = (error is not None) or not reached
    r.verdict(
        reproduced,
        "character could not reach a target that is only fenced by walkable belts"
        if reproduced
        else "move_to reached the belt-enclosed target: the collision_mask does list "
        "transport_belt (raw request without the own-entities flag says "
        + ("not_found -> belts ARE obstacles to the mask" if mask_blocks else f"{status}")
        + "), but move_to always sets allow_paths_through_own_entities=True, which "
        "masks the problem for own-force belts",
    )
    return reproduced


def repro_376(inst):
    r = Reporter("#376", "request_path fails from inside a collision box")
    ns = inst.namespace
    from fle.env.entities import Position

    char_pos = lua(
        inst,
        "local c = storage.agent_characters[1] rcon.print(c.position.x..','..c.position.y)",
    )
    cx, cy = (float(v) for v in char_pos.split(","))
    # Script-spawn a furnace exactly on the character (ignores collision checks)
    lua(
        inst,
        f'game.surfaces[1].create_entity{{name="stone-furnace", '
        f"position={{x={cx},y={cy}}}, force=game.forces.player}}",
    )
    overlap = lua(
        inst,
        f"local c = storage.agent_characters[1] "
        f'local e = game.surfaces[1].find_entities_filtered{{name="stone-furnace", '
        f"position={{x={cx},y={cy}}}, radius=1}}[1] "
        f"rcon.print(e and ('furnace overlaps character at '..e.position.x..','..e.position.y) or 'no furnace')",
    )
    r.note(f"spawned collision box on top of character: {overlap}")

    error = None
    try:
        final = ns.move_to(Position(x=cx + 8, y=cy))
        r.note(f"move_to(+8,0) returned {final}")
    except Exception as e:
        error = str(e)
        r.note(f"move_to(+8,0) raised: {error}")

    new_pos = lua(
        inst,
        "local c = storage.agent_characters[1] rcon.print(c.position.x..','..c.position.y)",
    )
    r.note(f"character position afterwards: ({new_pos})")
    nx, ny = (float(v) for v in new_pos.split(","))
    # Tolerance note: move_to routinely stops up to ~2 tiles short of the
    # requested goal (request_path nudges the goal via avoid_entity /
    # find_non_colliding_position), independent of this issue.  The #376
    # failure mode we are probing for is a PERMANENT failure (no movement /
    # path error), so count any substantial approach as an escape.
    moved = abs(nx - (cx + 8)) <= 3.0

    # Variant: a NEUTRAL-force collision box (e.g. a big rock) on top of the
    # character; allow_paths_through_own_entities does not cover other forces.
    rock = lua(
        inst,
        f'local e = game.surfaces[1].find_entities_filtered{{name="stone-furnace"}}[1] '
        f"if e then e.destroy() end "
        f'local made = game.surfaces[1].create_entity{{name="huge-rock", '
        f'position={{x={nx},y={ny}}}, force="neutral"}} '
        f"rcon.print(made and ('huge-rock (force '..made.force.name..') created at '"
        f"..made.position.x..','..made.position.y) or 'no rock spawned')",
    )
    r.note(f"neutral-force variant: {rock}")
    neutral_error = None
    if "no rock" not in rock:
        try:
            final2 = ns.move_to(Position(x=nx + 8, y=ny))
            r.note(f"move_to from inside neutral rock returned {final2}")
        except Exception as e:
            neutral_error = str(e)
            r.note(f"move_to from inside neutral rock raised: {neutral_error}")

    reproduced = (error is not None) or not moved or (neutral_error is not None)
    r.verdict(
        reproduced,
        "movement fails once the character is inside a collision box; the failure "
        "surfaces only as a generic path error/timeout, with no hint of the cause"
        if reproduced
        else "character escaped both own-force and neutral-force collision boxes "
        "(own-force via allow_paths_through_own_entities=True; the engine "
        "pathfinder also recovered from the neutral-force start)",
    )
    return reproduced


def repro_380(inst):
    r = Reporter("#380", "off-grid placement blames 'something in the way'")
    ns = inst.namespace
    from fle.env.game_types import Prototype
    from fle.env.entities import Position
    from fle.env import Direction

    spot = Position(x=30.5, y=30.5)
    nearby = lua(
        inst,
        "rcon.print(#game.surfaces[1].find_entities_filtered{"
        "area={{28,28},{33,33}}} .. ' entities near (30.5,30.5)')",
    )
    r.note(f"ground truth around target: {nearby}")
    ns.move_to(Position(x=28, y=28))

    can = ns.can_place_entity(Prototype.StoneFurnace, Direction.UP, spot)
    r.note(f"can_place_entity(stone-furnace at (30.5,30.5)) -> {can}")

    error = None
    try:
        placed = ns.place_entity(Prototype.StoneFurnace, Direction.UP, spot)
        r.note(f"place_entity(stone-furnace at (30.5,30.5)) SUCCEEDED at {placed.position}")
    except Exception as e:
        error = str(e)
        r.note(f"place_entity(stone-furnace at (30.5,30.5)) raised: {error}")

    misleading = error is not None and "in the way" in error and "grid" not in error.lower()
    r.verdict(
        misleading,
        "in an empty area, a half-integer 2x2 placement fails with a collision-style "
        "message and no mention of tile-grid alignment"
        if misleading
        else "placement either succeeded (snapped) or the error explains alignment",
    )
    return misleading


def main():
    inst = connect(inventory={"transport-belt": 50, "stone-furnace": 2})
    results = {}
    try:
        inst.reset()
        results["375"] = repro_375(inst)
        inst.reset()
        results["376"] = repro_376(inst)
        inst.reset()
        results["380"] = repro_380(inst)
    finally:
        inst.cleanup()
    print(f"summary: {results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
