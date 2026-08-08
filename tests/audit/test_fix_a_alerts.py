"""Integration tests for FIX A (upstream issue #378).

Old behavior (fle/env/mods/alerts.lua before the fix):
  * get_alerts(seconds) returned only alerts OLDER than the window
    -> test_fresh_alert_visible_within_window FAILED (empty result).
  * get_alerts deleted every alert it returned
    -> test_read_is_not_destructive FAILED (second read empty).
  * alerts were never invalidated after repair
    -> test_repaired_issue_is_invalidated FAILED (stale 'out of fuel').

New behavior: get_warnings(seconds) returns CURRENT alerts seen within the
window, reads are non-destructive, and repaired/removed entities drop out.
"""

from fle.env import Direction
from fle.env.entities import Position
from fle.env.game_types import Prototype


def _broken_furnace(instance):
    """Place a furnace with ore but no fuel -> 'out of fuel' alert."""
    ns = instance.namespace
    furnace = ns.place_entity(Prototype.StoneFurnace, Direction.UP, Position(x=2, y=0))
    ns.insert_item(Prototype.IronOre, furnace, 10)
    # Alert scanner runs every 60 ticks; give it a few scans.
    ns.sleep(3)
    return furnace


def test_fresh_alert_visible_within_window(instance):
    _broken_furnace(instance)
    warnings = instance.get_warnings(seconds=10)
    assert warnings, "a current alert must be visible inside the age window"
    assert any("out of fuel" in w for w in warnings), warnings


def test_read_is_not_destructive(instance):
    _broken_furnace(instance)
    first = instance.get_warnings(seconds=10)
    second = instance.get_warnings(seconds=10)
    assert first, "expected the out-of-fuel alert on first read"
    assert second, (
        "second read must still return the alert while the issue persists "
        f"(first={first!r}, second={second!r})"
    )
    assert any("out of fuel" in w for w in second), second


def test_repaired_issue_is_invalidated(instance):
    ns = instance.namespace
    furnace = _broken_furnace(instance)
    assert any("out of fuel" in w for w in instance.get_warnings(seconds=10))

    # Repair: give the furnace fuel; it starts smelting.
    ns.insert_item(Prototype.Coal, furnace, 10)
    ns.sleep(3)

    warnings = instance.get_warnings(seconds=10)
    assert not any("out of fuel" in w for w in warnings), (
        f"repaired issue must not be reported any more, got {warnings!r}"
    )


def test_removed_entity_alert_is_invalidated(instance):
    ns = instance.namespace
    furnace = _broken_furnace(instance)
    assert instance.get_warnings(seconds=10)

    ns.pickup_entity(Prototype.StoneFurnace, furnace.position)
    ns.sleep(2)

    warnings = instance.get_warnings(seconds=10)
    assert not any("out of fuel" in w for w in warnings), (
        f"alert for a removed entity must be invalidated, got {warnings!r}"
    )
