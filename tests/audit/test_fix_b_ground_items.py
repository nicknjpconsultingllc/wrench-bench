"""Integration tests for FIX B (upstream issue #379).

Old behavior: get_entities() queried find_entities_filtered with
force=player.force, so neutral-force ground items ("item-on-ground",
type "item-entity") were invisible although they block placement
(can_place_entity returns False on tiles holding them).
  -> test_ground_items_visible FAILED (empty result).

New behavior: get_entities() additionally returns ground items as minimal
ItemOnGround objects (name/position/item/count); normal entity
serialization is unchanged, and prototype-filtered queries only include
ground items when Prototype.ItemOnGround is requested.
"""

from fle.env import Direction
from fle.env.entities import ItemOnGround, Position
from fle.env.game_types import Prototype


def _drop_items(instance, x, y, item="iron-plate", count=7):
    instance.rcon_client.send_command(
        "/silent-command game.surfaces[1].create_entity{name='item-on-ground', "
        f"position={{x={x},y={y}}}, stack={{name='{item}', count={count}}}}}"
    )


def test_ground_items_visible(instance):
    ns = instance.namespace
    _drop_items(instance, 3.2, 3.2)
    _drop_items(instance, 2.6, 2.6)

    entities = ns.get_entities(position=Position(x=3, y=3), radius=5)
    ground = [e for e in entities if isinstance(e, ItemOnGround)]
    assert len(ground) == 2, f"expected 2 ground items, got {entities!r}"
    assert all(g.item == "iron-plate" and g.count == 7 for g in ground), ground
    assert all(abs(g.position.x - 3) < 2 and abs(g.position.y - 3) < 2 for g in ground)


def test_ground_items_explain_blocked_placement(instance):
    ns = instance.namespace
    _drop_items(instance, 3.2, 3.2)

    blocked = ns.can_place_entity(
        Prototype.StoneFurnace, Direction.UP, Position(x=3, y=3)
    )
    assert blocked is False, "ground item should block placement"

    # The blocking object is now observable:
    entities = ns.get_entities(position=Position(x=3, y=3), radius=3)
    assert any(isinstance(e, ItemOnGround) for e in entities), entities


def test_normal_entities_still_serialized(instance):
    ns = instance.namespace
    furnace = ns.place_entity(Prototype.StoneFurnace, Direction.UP, Position(x=6, y=6))
    _drop_items(instance, 8.5, 6.0)

    entities = ns.get_entities(position=Position(x=7, y=6), radius=6)
    names = sorted(getattr(e, "name", "?") for e in entities)
    assert "stone-furnace" in names, names
    assert "item-on-ground" in names, names
    furnace_entities = [
        e for e in entities if getattr(e, "name", "") == "stone-furnace"
    ]
    assert furnace_entities[0].position == furnace.position


def test_prototype_filter_excludes_ground_items(instance):
    ns = instance.namespace
    ns.place_entity(Prototype.StoneFurnace, Direction.UP, Position(x=6, y=6))
    _drop_items(instance, 8.5, 6.0)

    only_furnaces = ns.get_entities(
        {Prototype.StoneFurnace}, position=Position(x=7, y=6), radius=6
    )
    assert only_furnaces, "furnace filter should return the furnace"
    assert not any(isinstance(e, ItemOnGround) for e in only_furnaces), only_furnaces

    only_items = ns.get_entities(
        {Prototype.ItemOnGround}, position=Position(x=7, y=6), radius=6
    )
    assert any(isinstance(e, ItemOnGround) for e in only_items), only_items
