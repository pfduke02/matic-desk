"""MaticService: produces every shape in docs/protocol.md.

The bridge (Swift app), MCP server and CLI only reshape or print what this
returns. Inject `robot_factory` and `ledger` to run it against fakes.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import (
    aliases_path,
    ledger_path,
    load_aliases,
    load_robot_config,
    read_robot_config,
    robot_config_path,
    save_aliases,
)
from .errors import MaticDeskError, invalid
from .ledger import Ledger, iso, normalize_time, parse_iso
from .maps import render_map
from .robot import MaticRobot, Robot, map_error
from .rooms import alias_conflict, norm, resolve_rooms, room_groups, sort_rooms
from .vendor.hermes_client.commands import CleaningMode, CoverageSetting, UserCommand
from .vendor.hermes_client.models import (
    CleaningSession,
    CleaningSessionRecord,
    FloorPlan,
    RobotOperationalState,
    RobotTelemetry,
)

log = logging.getLogger(__name__)

ERROR_LABELS = {205: "bag missing", 206: "bag full"}
MODES = {"vacuum": CleaningMode.VACUUM, "mop": CleaningMode.MOP, "both": CleaningMode.BOTH}
COVERAGES = {
    "quick": CoverageSetting.QUICK,
    "standard": CoverageSetting.OPTIMAL,
    "heavy_duty": CoverageSetting.HEAVY_DUTY,
}
COMMANDS = {
    "pause": UserCommand.PAUSE,
    "resume": UserCommand.RESUME,
    "stop": UserCommand.STOP,
    "dock": UserCommand.DOCK,
}
SETTINGS = ("water_flow", "deep_mop", "pet_waste", "child_lock", "voice")
IDLE = frozenset({"docked", "charging", "ready"})
ON_MISSION = frozenset({"cleaning", "paused", "returning"})
_TRUE = {"on", "true", "yes", "1", "enable", "enabled"}
_FALSE = {"off", "false", "no", "0", "disable", "disabled"}
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def default_robot_factory() -> Robot:
    return MaticRobot(load_robot_config())


def utcnow() -> datetime:
    return datetime.now(UTC)


def robot_config_stamp() -> tuple[int, int] | None:
    """Changes whenever robot.json is rewritten (e.g. by `matic-desk pair`)."""
    try:
        info = robot_config_path().stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


# ---------------------------------------------------------------- shape helpers


def status_from_state(state: RobotOperationalState, robot_name: str | None, now: datetime) -> dict[str, Any]:
    return {
        "connected": True,
        "robot_name": robot_name,
        "activity": state.activity.value,
        "battery": state.battery_percentage,
        "charging": state.charging,
        "low_charge": state.low_charge,
        "current_room": room_label(state.current_area),
        "errors": [{"code": code, "label": ERROR_LABELS.get(code, f"error {code}")} for code in state.error_codes],
        "bag_full": state.bag_full,
        "bag_missing": state.bag_missing,
        "firmware": state.software_version,
        "updated_at": iso(now),
        "message": None,
    }


def offline_status(message: str, robot_name: str | None, now: datetime) -> dict[str, Any]:
    return {
        "connected": False,
        "robot_name": robot_name,
        "activity": "offline",
        "battery": None,
        "charging": None,
        "low_charge": None,
        "current_room": None,
        "errors": [],
        "bag_full": None,
        "bag_missing": None,
        "firmware": None,
        "updated_at": iso(now),
        "message": message,
    }


def room_label(area: str | None) -> str | None:
    """`"the Kitchen"` -> `"Kitchen"`."""
    if not area or not area.strip():
        return None
    label = area.strip()
    if label[:4].casefold() == "the ":
        label = label[4:].strip()
    return label or None


def settings_from_telemetry(telemetry: RobotTelemetry) -> dict[str, Any]:
    flow = telemetry.water_flow_factor
    return {
        "water_flow": round(flow, 1) if flow is not None else None,
        "deep_mop": telemetry.deep_mop_enabled,
        "pet_waste": telemetry.pet_waste_enabled,
        "child_lock": telemetry.child_lock_enabled,
        "voice": telemetry.voice_enabled,
    }


def session_from_record(record: CleaningSessionRecord) -> dict[str, Any]:
    session = record.session
    return {
        "key": record.key.hex(),
        "started_at": normalize_time(session.started_at),
        "ended_at": normalize_time(session.ended_at),
        "duration_seconds": session.duration_seconds,
        "vacuum_seconds": _mode_seconds(session, "vacuum"),
        "mop_seconds": _mode_seconds(session, "mop"),
        "rooms": list(session.rooms),
        "modes": sorted({result.cleaning_mode for result in session.mode_results}),
        "completed": session.completed,
    }


def _mode_seconds(session: CleaningSession, mode: str) -> int | None:
    if not session.mode_results:
        return None  # the robot did not say
    values = [r.duration_seconds for r in session.mode_results if r.cleaning_mode == mode]
    known = [value for value in values if value is not None]
    if values and not known:
        return None
    return int(sum(known))


# ---------------------------------------------------------------- input parsing


def parse_when(value: str | None, now: datetime) -> datetime:
    """`YYYY-MM-DD`, `YYYY-MM-DDTHH:MM` or full ISO 8601; local time if no offset.

    Today's date alone means now, not local midnight: "I swapped it today" must
    not move this morning's runs (and bag-full event) onto the new bag.
    """
    if value is None or not str(value).strip():
        return now
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise invalid(
            f"Couldn't read the date {text!r}. Use YYYY-MM-DD, YYYY-MM-DDTHH:MM or a full ISO 8601 time."
        ) from None
    if moment.tzinfo is None:
        if _DATE_ONLY.fullmatch(text) and moment.date() == now.astimezone().date():
            return now
        moment = moment.astimezone()  # interpret as local time
    if moment > now + timedelta(minutes=1):
        raise invalid(f"{text!r} is in the future; give the date the bag was actually changed.")
    return moment.astimezone(UTC)


def parse_bool(value: Any, name: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise invalid(f"{name} must be on or off, not {value!r}.")


def parse_water_flow(value: Any) -> float:
    if isinstance(value, bool):
        raise invalid("water_flow must be a number from 0.5 to 2.0.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise invalid(f"water_flow must be a number from 0.5 to 2.0, not {value!r}.") from None
    if not math.isfinite(number) or not 0.5 - 1e-9 <= number <= 2.0 + 1e-9:
        raise invalid("water_flow must be from 0.5 to 2.0.")
    if abs(number * 10 - round(number * 10)) > 1e-6:
        raise invalid("water_flow uses 0.1 steps (for example 1.2).")
    return round(number, 1)


def _choice(value: Any, options: Iterable[str], name: str) -> str:
    choices = list(options)
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in choices:
        raise invalid(f"{name} must be one of {', '.join(choices)}; got {value!r}.")
    return key


def _room_list(rooms: Any) -> list[str]:
    if rooms is None:
        return []
    if isinstance(rooms, str):
        rooms = [rooms]
    if not isinstance(rooms, (list, tuple)):
        raise invalid("rooms must be a list of room names.")
    return [str(room) for room in rooms if str(room).strip()]


# ---------------------------------------------------------------------- service


class MaticService:
    def __init__(
        self,
        robot_factory: Callable[[], Robot] = default_robot_factory,
        ledger: Ledger | None = None,
        *,
        aliases_file: Path | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        settle_seconds: float = 8.0,
    ) -> None:
        self._robot_factory = robot_factory
        self._robot: Robot | None = None
        self._robot_stamp: tuple[int, int] | None = None
        self._ledger_obj = ledger
        self._owns_ledger = ledger is None
        self._aliases_file = aliases_file
        self._clock = clock
        self._sleep = sleep
        self.settle_seconds = settle_seconds

    # ---- plumbing

    async def _get_robot(self) -> Robot:
        """The shared robot, rebuilt when robot.json changes (re-pairing takes effect live)."""
        stamp = robot_config_stamp()
        if self._robot is not None and stamp != self._robot_stamp:
            log.info("robot.json changed; reconnecting with the new pairing")
            stale, self._robot = self._robot, None
            try:
                await stale.close()
            except Exception as err:  # closing a dead connection must not block the new one
                log.info("Closing the previous robot connection failed: %s", err)
        if self._robot is None:
            try:
                self._robot = self._robot_factory()
            except MaticDeskError:
                raise
            except Exception as err:
                raise map_error(err) from err
            self._robot_stamp = stamp
        return self._robot

    @property
    def ledger(self) -> Ledger:
        if self._ledger_obj is None:
            self._ledger_obj = Ledger(ledger_path())
        return self._ledger_obj

    def _aliases(self) -> dict[str, list[str]]:
        return load_aliases(self._aliases_file or aliases_path())

    def _observe(self, state: RobotOperationalState, robot: Robot) -> dict[str, Any]:
        now = self._clock()
        status = status_from_state(state, robot.name, now)
        try:
            for kind in self.ledger.record_bag_flags(status["bag_full"], status["bag_missing"], now):
                log.info("Bag event: %s", kind)
        except sqlite3.Error as err:
            log.warning("Could not record bag flags: %s", err)
        return status

    async def _settle(self, robot: Robot, done: Callable[[str], bool], seconds: float) -> str:
        """Re-read activity about once a second until `done(activity)` or time runs out."""
        activity = "offline"
        for attempt in range(max(0, int(seconds)) + 1):
            if attempt:
                await self._sleep(1)
            try:
                activity = self._observe(await robot.get_state(), robot)["activity"]
            except MaticDeskError:
                continue
            if done(activity):
                break
        return activity

    async def _robot_sessions(self) -> list[dict[str, Any]]:
        records = await (await self._get_robot()).get_session_records()
        return [session_from_record(record) for record in records]

    async def _refresh_history(self) -> None:
        """Copy the robot's runs into the ledger."""
        self.ledger.ingest(await self._robot_sessions(), self._clock())

    def _import_skill_swap(self) -> None:
        """Once per ledger: adopt the original skill's `bag_swapped_at` (robot.json) as the first swap."""
        try:
            moment = parse_iso(str(read_robot_config().get("bag_swapped_at") or ""))
            if self.ledger.import_swap_once(moment, self._clock()):
                log.info("Imported the bag swap date recorded in robot.json")
        except sqlite3.Error as err:
            log.warning("Could not import the bag swap date from robot.json: %s", err)

    def _rooms_payload(self, plan: FloorPlan) -> dict[str, Any]:
        names = [room.name for room in plan.rooms]
        return {
            "floor": plan.floor_label,
            "rooms": [{"name": name} for name in sort_rooms(names)],
            "groups": room_groups(names),
            "aliases": self._aliases(),
        }

    # ---- contract

    async def status(self) -> dict[str, Any]:
        """Never raises for connectivity: returns `activity: "offline"` instead."""
        try:
            robot = await self._get_robot()
            state = await robot.get_state()
        except MaticDeskError as err:
            name = self._robot.name if self._robot is not None else None
            return offline_status(err.message, name, self._clock())
        return self._observe(state, robot)

    async def status_updates(self) -> AsyncIterator[dict[str, Any]]:
        """Live Status from the robot's state stream (raises MaticDeskError when it drops)."""
        robot = await self._get_robot()
        async for state in robot.subscribe_state():
            yield self._observe(state, robot)

    async def settings(self) -> dict[str, Any]:
        return settings_from_telemetry(await (await self._get_robot()).get_telemetry())

    async def rooms(self) -> dict[str, Any]:
        return self._rooms_payload(await (await self._get_robot()).get_floor_plan())

    async def history(self, limit: int = 10) -> dict[str, Any]:
        if isinstance(limit, bool):
            raise invalid("limit must be a positive whole number.")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise invalid("limit must be a positive whole number.") from None
        if limit < 1:
            raise invalid("limit must be a positive whole number.")
        sessions = await self._robot_sessions()
        try:
            self.ledger.ingest(sessions, self._clock())
            # Every run seen so far: a cut-short read or a run the robot has
            # already forgotten does not drop out of the history.
            sessions = self.ledger.sessions()
        except sqlite3.Error as err:
            log.warning("Ledger unavailable; history shows only the robot's own list: %s", err)
        sessions.sort(key=lambda s: s["started_at"] or "", reverse=True)
        return {"sessions": sessions[:limit]}

    async def bag_report(self) -> dict[str, Any]:
        status = await self.status()
        if status["connected"]:
            try:
                await self._refresh_history()
            except (MaticDeskError, sqlite3.Error) as err:  # best effort: report from the ledger as it is
                log.info("Bag report without a history refresh: %s", err)
        self._import_skill_swap()
        return self.ledger.bag_report(self._clock(), status)

    async def record_bag_swap(self, at: str | None = None, note: str | None = None) -> dict[str, Any]:
        now = self._clock()
        moment = parse_when(at, now)
        note = str(note).strip() or None if note is not None else None
        replaced = self.ledger.record_swap(moment, note, now)
        log.info("Recorded a bag swap at %s (it replaced %d swap(s) at or after it)", iso(moment), replaced)
        return await self.bag_report()

    async def clean(
        self,
        rooms: list[str] | None = None,
        whole_floor: bool = False,
        mode: str = "both",
        coverage: str = "standard",
        ordered: bool = False,
    ) -> dict[str, Any]:
        mode = _choice(mode, MODES, "mode")
        coverage = _choice(coverage, COVERAGES, "coverage")
        whole_floor = parse_bool(whole_floor, "whole_floor")
        ordered = parse_bool(ordered, "ordered")
        wanted = _room_list(rooms)
        if not wanted and not whole_floor:
            raise invalid("Say which rooms to clean, or set whole_floor to clean the whole floor.")

        robot = await self._get_robot()
        plan = await robot.get_floor_plan()
        if not plan.rooms:
            raise MaticDeskError("robot_error", "The robot's map has no rooms yet.")
        if whole_floor:
            targets = list(plan.rooms)
        else:
            names = resolve_rooms(wanted, [room.name for room in plan.rooms], self._aliases())
            by_name = {room.name: room for room in plan.rooms}
            targets = [by_name[name] for name in names]

        await robot.start_coverage(
            plan, [room.protocol_id for room in targets], MODES[mode], COVERAGES[coverage], ordered
        )
        log.info("Started %s/%s cleaning of %d room(s)", mode, coverage, len(targets))
        activity = await self._settle(robot, lambda a: a not in IDLE, self.settle_seconds)
        return {
            "accepted": True,
            "rooms": [room.name for room in targets],
            "mode": mode,
            "coverage": coverage,
            "ordered": ordered,
            "activity": activity,
        }

    async def command(self, name: str) -> dict[str, Any]:
        name = _choice(name, COMMANDS, "command")
        robot = await self._get_robot()
        before = (await robot.get_state()).activity.value
        await robot.send_command(COMMANDS[name])
        log.info("Sent %s", name)
        activity = await self._settle(robot, lambda a: a != before, min(5.0, self.settle_seconds))
        return {"sent": name, "activity": activity}

    async def set_setting(self, setting: str, value: Any) -> dict[str, Any]:
        setting = _choice(setting, SETTINGS, "setting")
        robot = await self._get_robot()
        if setting == "water_flow":
            await robot.set_water_flow(parse_water_flow(value))
        elif setting == "deep_mop":
            await robot.set_deep_mop(parse_bool(value, setting))
        else:
            await robot.set_binary_setting(setting, parse_bool(value, setting))
        log.info("Changed setting %s", setting)
        return await self.settings()

    async def map_png(self, route: bool = True) -> bytes:
        route = parse_bool(route, "route")
        robot = await self._get_robot()
        plan = await robot.get_floor_plan()
        state = await robot.get_state()
        pose = await robot.get_pose()
        points = await robot.get_route(plan) if route and state.activity.value in ON_MISSION else ()
        return await asyncio.to_thread(render_map, plan, pose, state.current_area, points)

    async def name_room(self, alias: str, rooms: list[str]) -> dict[str, Any]:
        alias = " ".join(str(alias or "").split())
        if not alias:
            raise invalid("Give the alias a name.")
        wanted = _room_list(rooms)
        if not wanted:
            raise invalid("List at least one room for the alias.")
        plan = await (await self._get_robot()).get_floor_plan()
        room_names = [room.name for room in plan.rooms]
        conflict = alias_conflict(alias, room_names)
        if conflict:
            raise invalid(f"{alias!r} can't be an alias: {conflict}. Pick another name.")
        aliases = self._aliases()
        names = resolve_rooms(wanted, room_names, aliases)
        aliases = {name: targets for name, targets in aliases.items() if norm(name) != norm(alias)}
        aliases[alias] = names
        save_aliases(aliases, self._aliases_file or aliases_path())
        return self._rooms_payload(plan)

    async def remove_room_alias(self, alias: str) -> dict[str, Any]:
        aliases = self._aliases()
        matches = [name for name in aliases if norm(name) == norm(str(alias or ""))]
        if not matches:
            known = ", ".join(sorted(aliases)) or "none"
            raise invalid(f"No alias called {alias!r}. Aliases: {known}.")
        plan = await (await self._get_robot()).get_floor_plan()
        for name in matches:
            del aliases[name]
        save_aliases(aliases, self._aliases_file or aliases_path())
        return self._rooms_payload(plan)

    async def close(self) -> None:
        if self._robot is not None:
            try:
                await self._robot.close()
            finally:
                self._robot = None
        if self._owns_ledger and self._ledger_obj is not None:
            self._ledger_obj.close()
            self._ledger_obj = None
