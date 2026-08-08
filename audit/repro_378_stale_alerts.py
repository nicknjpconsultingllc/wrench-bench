"""Repro for upstream issue #378: get_alerts is inverted, destructive, and stale.

fle/env/mods/alerts.lua:
  * on_tick (every 60 ticks) records an alert per broken entity, keyed by
    entity+position, only if no alert with that key exists.  The alert is
    NEVER removed when the underlying issue is repaired.
  * storage.get_alerts(seconds) returns only alerts OLDER than the window
    (current_tick - alert.tick > 60*seconds) and DELETES every alert it
    returns.

Consequences demonstrated below via FactorioInstance.get_warnings(seconds)
(fle/env/instance.py):

  (a) A furnace that has ore but no fuel raises an 'out of fuel' alert;
      get_warnings(10) called seconds later returns NOTHING because the
      alert is younger than the 10s window (inverted filter).
      Expected: current alerts are visible immediately.
  (b) Once the alert is old enough to be returned, reading it CONSUMES it:
      an immediate second call returns nothing although the furnace is
      still broken.  Expected: reads are non-destructive.
  (c) After repairing the furnace (inserting coal), the previously recorded
      alert is still returned.  Expected: repaired issues stop being
      reported.

PASS = all three behaviors observed (bug reproduced).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, Reporter


def main():
    r = Reporter("#378", "alerts stale + destructive reads")

    inst = connect(inventory={"stone-furnace": 1, "iron-ore": 20, "coal": 20})
    try:
        inst.reset()
        inst.set_speed(5)
        ns = inst.namespace

        from fle.env.game_types import Prototype
        from fle.env.entities import Position
        from fle.env import Direction

        furnace = ns.place_entity(
            Prototype.StoneFurnace, Direction.UP, Position(x=2, y=0)
        )
        # Ore but no fuel -> 'out of fuel' issue in alerts.lua get_issues()
        furnace = ns.insert_item(Prototype.IronOre, furnace, 10)
        r.note(f"placed stone-furnace at {furnace.position} with 10 iron-ore, no fuel")

        # Let the on_tick scanner (every 60 ticks) register the alert.
        ns.sleep(3)

        # (a) fresh alert should be visible, but the window is inverted
        w_fresh = inst.get_warnings(seconds=10)
        r.note(f"get_warnings(10) ~3s after issue arose -> {w_fresh!r}")
        fresh_invisible = len(w_fresh) == 0

        # age the alert past a 2 second window
        ns.sleep(4)
        w_old = inst.get_warnings(seconds=2)
        r.note(f"get_warnings(2) after ~7s -> {w_old!r}")
        old_visible = len(w_old) > 0 and any("out of fuel" in w for w in w_old)

        # (b) destructive read: furnace is STILL broken, immediate re-read
        w_again = inst.get_warnings(seconds=2)
        r.note(f"immediate second get_warnings(2), furnace still broken -> {w_again!r}")
        destructive = old_visible and len(w_again) == 0

        # let the scanner re-register the alert (entry was deleted by the read)
        ns.sleep(3)

        # (c) repair the issue: insert coal -> furnace starts burning
        furnace = ns.get_entities({Prototype.StoneFurnace})[0]
        ns.insert_item(Prototype.Coal, furnace, 10)
        ns.sleep(4)
        furnace = ns.get_entities({Prototype.StoneFurnace})[0]
        r.note(f"inserted 10 coal; furnace status now: {furnace.status}, warnings on entity: {furnace.warnings}")

        w_stale = inst.get_warnings(seconds=2)
        r.note(f"get_warnings(2) ~4s AFTER repair -> {w_stale!r}")
        stale_after_repair = len(w_stale) > 0 and any(
            "out of fuel" in w for w in w_stale
        )

        reproduced = fresh_invisible and old_visible and destructive and stale_after_repair
        r.note(
            f"(a) fresh alert invisible in window: {fresh_invisible}; "
            f"(b) read consumed alert: {destructive}; "
            f"(c) stale alert readable after repair: {stale_after_repair}"
        )
        r.verdict(
            reproduced,
            "get_alerts returns only alerts OLDER than the window, deletes on read, "
            "and never invalidates repaired issues"
            if reproduced
            else "current get_alerts behaves correctly (fresh visible / non-destructive / repaired invalidated)",
        )
        return 0 if reproduced else 1
    finally:
        inst.cleanup()


if __name__ == "__main__":
    sys.exit(main())
