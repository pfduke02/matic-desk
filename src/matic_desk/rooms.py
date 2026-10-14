"""Room groups and name resolution (docs/protocol.md, "Room resolution")."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .errors import MaticDeskError

WHOLE_FLOOR_WORDS = frozenset({"all", "everything", "whole floor", "whole house"})
_SEGMENT = re.compile(r"^(?P<base>.*\S)\s+(?P<number>\d+)$")


def norm(value: str) -> str:
    """Case- and whitespace-insensitive key."""
    return " ".join(value.split()).casefold()


def natural_key(name: str) -> tuple[str, int, str]:
    match = _SEGMENT.match(name.strip())
    if match:
        return norm(match["base"]), int(match["number"]), name
    return norm(name), -1, name


def sort_rooms(names: Iterable[str]) -> list[str]:
    return sorted(names, key=natural_key)


def room_groups(names: Iterable[str]) -> dict[str, list[str]]:
    """Collapse segments named `<Base> <digits>` under `<Base>`."""
    groups: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for name in sort_rooms(names):
        match = _SEGMENT.match(name.strip())
        if not match:
            continue
        key = norm(match["base"])
        label = labels.setdefault(key, " ".join(match["base"].split()))
        groups.setdefault(label, []).append(name)
    return groups


def resolve_rooms(
    wanted: Iterable[str],
    room_names: Iterable[str],
    aliases: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """Resolve user words to exact room names; order kept, duplicates dropped."""
    rooms = list(room_names)
    by_name = {norm(name): name for name in rooms}
    alias_map = {norm(alias): list(targets) for alias, targets in (aliases or {}).items()}
    groups = {norm(base): members for base, members in room_groups(rooms).items()}

    resolved: list[str] = []
    for raw in wanted:
        key = norm(str(raw))
        if not key:
            continue
        if key in alias_map:
            matches = []
            for target in alias_map[key]:
                room = by_name.get(norm(target))
                if room is None:
                    raise MaticDeskError(
                        "room_not_found",
                        f"Alias {raw!r} points to {target!r}, which is not on the current map. "
                        + _choices(rooms, aliases),
                    )
                matches.append(room)
        elif key in by_name:
            matches = [by_name[key]]
        elif key in groups:
            matches = groups[key]
        elif key.endswith("s") and key[:-1] in groups:
            matches = groups[key[:-1]]
        elif key in WHOLE_FLOOR_WORDS:
            matches = sort_rooms(rooms)
        else:
            raise MaticDeskError("room_not_found", f"No room called {raw!r}. " + _choices(rooms, aliases))
        for room in matches:
            if room not in resolved:
                resolved.append(room)
    return resolved


def alias_conflict(alias: str, room_names: Iterable[str]) -> str | None:
    """Why `alias` would shadow a room, a group or a whole-floor word, else None.

    Aliases resolve first, so allowing these would silently change what an exact
    room name (as the menu bar app sends) or "all" cleans.
    """
    key = norm(alias)
    names = list(room_names)
    if key in {norm(name) for name in names}:
        return "a room on the map already has that name"
    groups = {norm(base) for base in room_groups(names)}
    if key in groups or (key.endswith("s") and key[:-1] in groups):
        return "it is the name of a room group"
    if key in WHOLE_FLOOR_WORDS:
        return "it already means the whole floor"
    return None


def _choices(rooms: list[str], aliases: Mapping[str, Iterable[str]] | None) -> str:
    text = "Rooms: " + (", ".join(sort_rooms(rooms)) or "none on the map") + "."
    if aliases:
        text += " Aliases: " + ", ".join(sorted(aliases)) + "."
    return text
