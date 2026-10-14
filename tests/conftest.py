"""Shared fakes. Tests never touch the network or the real data directory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from matic_desk.errors import MaticDeskError
from matic_desk.ledger import Ledger
from matic_desk.service import MaticService
from matic_desk.vendor.hermes_client.models import (
    CleaningModeResult,
    CleaningSession,
    CleaningSessionRecord,
    FloorPlan,
    RobotOperationalState,
    RobotPose,
    RobotTelemetry,
    Room,
)

# Invented placeholder map; not anyone's real home.
ROOM_NAMES = ("Kitchen 1", "Kitchen 2", "Living Room 1", "Living Room 2", "Office", "Hallway", "Bedroom")
NOW = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every default path at a temp dir; robot.json does not exist (not paired)."""
    monkeypatch.setenv("MATIC_DESK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MATIC_CONFIG", str(tmp_path / "missing-robot.json"))


def make_state(**changes: Any) -> RobotOperationalState:
    base = RobotOperationalState(
        battery_percentage=80,
        state_codes=(),
        error_codes=(),
        charging_idle=True,
        charging=False,
        low_charge=False,
        paused=False,
        cleaning=False,
        returning=False,
        software_version="v1.2.3",
        current_area=None,
        bag_full=False,
        bag_missing=False,
    )
    return replace(base, **changes)


CLEANING = {"charging_idle": False, "cleaning": True, "current_area": "the Living Room 1"}


def make_plan(names: tuple[str, ...] = ROOM_NAMES) -> FloorPlan:
    rooms = tuple(
        Room(
            id=f"room-{index}",
            name=name,
            protocol_id=str(uuid5(NAMESPACE_URL, f"room/{name}")),
            id_wire=b"",
            boundary=((index * 2.0, 0.0), (index * 2.0 + 2.0, 0.0), (index * 2.0 + 2.0, 3.0), (index * 2.0, 3.0)),
        )
        for index, name in enumerate(names)
    )
    return FloorPlan(
        mission_id=7,
        partition_protocol_id=str(uuid5(NAMESPACE_URL, "partition")),
        partition_id_wire=b"",
        rooms=rooms,
    )


def make_record(
    key: int,
    started_at: str,
    *,
    duration: int = 900,
    vacuum: int | None = 300,
    mop: int | None = 400,
    rooms: tuple[str, ...] = ("Kitchen 1",),
    completed: bool | None = True,
) -> CleaningSessionRecord:
    results = []
    for room in rooms:
        if vacuum is not None:
            results.append(CleaningModeResult(room, "vacuum", "completed", vacuum // len(rooms)))
        if mop is not None:
            results.append(CleaningModeResult(room, "mop", "completed", mop // len(rooms)))
    session = CleaningSession(
        started_at=started_at,
        ended_at=started_at,
        duration_seconds=duration,
        rooms=rooms,
        room_durations=(),
        completed=completed,
        mode_results=tuple(results),
    )
    return CleaningSessionRecord(key.to_bytes(22, "big"), session)


class FakeRobot:
    name = "matic-abcd-efgh"

    def __init__(self, **options: Any) -> None:
        self.state: RobotOperationalState = options.get("state") or make_state()
        self.plan: FloorPlan = options.get("plan") or make_plan()
        self.records: list[CleaningSessionRecord] = list(options.get("records", []))
        self.telemetry = options.get("telemetry") or RobotTelemetry(
            water_flow_factor=1.0000001,
            deep_mop_enabled=True,
            pet_waste_enabled=False,
            child_lock_enabled=False,
            voice_enabled=True,
        )
        self.stream: list[RobotOperationalState] = list(options.get("stream", []))
        self.offline = options.get("offline", False)
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    def _check(self) -> None:
        if self.offline:
            raise MaticDeskError("offline", "Can't reach the robot (test).")

    async def get_state(self) -> RobotOperationalState:
        self._check()
        return self.state

    async def get_telemetry(self) -> RobotTelemetry:
        self._check()
        return self.telemetry

    async def get_session_records(self) -> tuple[CleaningSessionRecord, ...]:
        self._check()
        return tuple(self.records)

    async def get_floor_plan(self) -> FloorPlan:
        self._check()
        return self.plan

    async def get_pose(self) -> RobotPose | None:
        return RobotPose(1.0, 1.0, 0.0)

    async def get_route(self, plan: FloorPlan, wait: float = 4.0) -> tuple[tuple[float, float], ...]:
        self.calls.append(("get_route",))
        return ((1.0, 1.0), (2.0, 1.5), (3.0, 2.0))

    async def start_coverage(self, plan, region_ids, cleaning_mode, coverage_setting, ordered) -> None:
        self._check()
        self.calls.append(("start_coverage", list(region_ids), cleaning_mode, coverage_setting, ordered))
        self.state = make_state(**CLEANING)

    async def send_command(self, command) -> None:
        self._check()
        self.calls.append(("send_command", command))
        if command.value == "dock":
            self.state = make_state(charging_idle=False, returning=True)

    async def set_water_flow(self, value: float) -> None:
        self.calls.append(("set_water_flow", value))

    async def set_deep_mop(self, enabled: bool) -> None:
        self.calls.append(("set_deep_mop", enabled))

    async def set_binary_setting(self, setting: str, enabled: bool) -> None:
        self.calls.append(("set_binary_setting", setting, enabled))

    async def subscribe_state(self) -> AsyncIterator[RobotOperationalState]:
        self._check()
        states, self.stream = self.stream, []  # one-shot, like a dropped stream
        for state in states:
            self.state = state
            yield state

    async def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def robot() -> FakeRobot:
    return FakeRobot()


@pytest.fixture
def service(robot: FakeRobot, clock: Clock, tmp_path: Path) -> MaticService:
    return MaticService(
        robot_factory=lambda: robot,
        ledger=Ledger(":memory:"),
        aliases_file=tmp_path / "aliases.json",
        clock=clock,
        sleep=no_sleep,
        settle_seconds=3,
    )
