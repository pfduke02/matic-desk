import pytest

from matic_desk.errors import MaticDeskError
from matic_desk.rooms import resolve_rooms, room_groups, sort_rooms

ROOMS = ["Living Room 2", "Kitchen 2", "Kitchen 1", "Living Room 1", "Office", "Room 10", "Room 2"]


def test_groups_collapse_numbered_segments():
    assert room_groups(ROOMS) == {
        "Kitchen": ["Kitchen 1", "Kitchen 2"],
        "Living Room": ["Living Room 1", "Living Room 2"],
        "Room": ["Room 2", "Room 10"],
    }


def test_natural_sort():
    assert sort_rooms(["Room 10", "Room 2", "Office"]) == ["Office", "Room 2", "Room 10"]


def test_exact_name_is_case_and_whitespace_insensitive():
    assert resolve_rooms(["  living   ROOM 1 "], ROOMS) == ["Living Room 1"]


def test_alias_wins_over_room_name():
    aliases = {"Office": ["Kitchen 1"], "downstairs": ["Kitchen 1", "Living Room 2"]}
    assert resolve_rooms(["office"], ROOMS, aliases) == ["Kitchen 1"]
    assert resolve_rooms(["Downstairs"], ROOMS, aliases) == ["Kitchen 1", "Living Room 2"]


def test_group_base_and_plural():
    assert resolve_rooms(["kitchen"], ROOMS) == ["Kitchen 1", "Kitchen 2"]
    assert resolve_rooms(["Kitchens"], ROOMS) == ["Kitchen 1", "Kitchen 2"]
    assert resolve_rooms(["living rooms"], ROOMS) == ["Living Room 1", "Living Room 2"]


@pytest.mark.parametrize("word", ["all", "Everything", "whole floor", "WHOLE  house"])
def test_whole_floor_words(word):
    assert resolve_rooms([word], ROOMS) == sort_rooms(ROOMS)


def test_order_preserved_and_duplicates_dropped():
    assert resolve_rooms(["Office", "kitchen", "Kitchen 2", "office"], ROOMS) == ["Office", "Kitchen 1", "Kitchen 2"]


def test_unknown_room_lists_rooms_and_aliases():
    with pytest.raises(MaticDeskError) as caught:
        resolve_rooms(["garage"], ROOMS, {"den": ["Office"]})
    assert caught.value.type == "room_not_found"
    assert "Kitchen 1" in caught.value.message and "den" in caught.value.message


def test_alias_to_room_no_longer_on_map():
    with pytest.raises(MaticDeskError) as caught:
        resolve_rooms(["den"], ROOMS, {"den": ["Study"]})
    assert caught.value.type == "room_not_found"
