import json
import os
import sqlite3
from datetime import UTC, datetime

import pytest
from conftest import CLEANING, FakeRobot, make_record, make_state

from matic_desk.errors import MaticDeskError
from matic_desk.ledger import Ledger
from matic_desk.service import MaticService, parse_when
from matic_desk.vendor.hermes_client.commands import CleaningMode, CoverageSetting, UserCommand

NOW = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- status


async def test_status_maps_robot_state(service, robot):
    robot.state = make_state(**CLEANING, error_codes=(206, 207), bag_full=True, battery_percentage=55)
    status = await service.status()
    assert status["connected"] is True
    assert status["activity"] == "cleaning"
    assert status["current_room"] == "Living Room 1"
    assert status["battery"] == 55
    assert status["errors"] == [{"code": 206, "label": "bag full"}, {"code": 207, "label": "error 207"}]
    assert status["bag_full"] is True and status["bag_missing"] is False
    assert status["firmware"] == "v1.2.3"
    assert status["robot_name"] == "matic-abcd-efgh"
    assert status["updated_at"] == "2026-10-14T14:00:00Z"
    assert status["message"] is None


async def test_status_offline_instead_of_raising(service, robot):
    robot.offline = True
    status = await service.status()
    assert status["connected"] is False
    assert status["activity"] == "offline"
    assert "reach" in status["message"]
    assert status["bag_full"] is None and status["battery"] is None


async def test_status_not_paired_is_offline(tmp_path):
    service = MaticService(ledger=Ledger(":memory:"))  # default factory; MATIC_CONFIG points nowhere
    status = await service.status()
    assert status["activity"] == "offline"
    assert "matic-desk pair" in status["message"]
    with pytest.raises(MaticDeskError) as caught:
        await service.rooms()
    assert caught.value.type == "not_paired"


async def test_status_records_bag_events(service, robot):
    robot.state = make_state(bag_full=None)
    await service.status()
    robot.state = make_state(bag_full=True)
    await service.status()
    assert len(service.ledger.events("bag_full_on")) == 1


# ---------------------------------------------------------------- clean


async def test_clean_needs_rooms_or_whole_floor(service, robot):
    with pytest.raises(MaticDeskError) as caught:
        await service.clean(rooms=[])
    assert caught.value.type == "invalid_request"
    assert robot.calls == []


async def test_clean_unknown_room(service, robot):
    with pytest.raises(MaticDeskError) as caught:
        await service.clean(rooms=["Garage"], mode="vacuum")
    assert caught.value.type == "room_not_found"
    assert "Kitchen 1" in caught.value.message
    assert robot.calls == []


@pytest.mark.parametrize("field, value", [("mode", "steam"), ("coverage", "turbo")])
async def test_clean_rejects_bad_mode_and_coverage(service, robot, field, value):
    with pytest.raises(MaticDeskError) as caught:
        await service.clean(rooms=["Office"], **{field: value})
    assert caught.value.type == "invalid_request"
    assert robot.calls == []


async def test_clean_group_vacuum_standard(service, robot):
    result = await service.clean(rooms=["living room"], mode="vacuum")
    [call] = [c for c in robot.calls if c[0] == "start_coverage"]
    _, region_ids, mode, coverage, ordered = call
    ids = {room.name: room.protocol_id for room in robot.plan.rooms}
    assert region_ids == [ids["Living Room 1"], ids["Living Room 2"]]
    assert mode is CleaningMode.VACUUM
    assert coverage is CoverageSetting.OPTIMAL
    assert ordered is False
    assert result == {
        "accepted": True,
        "rooms": ["Living Room 1", "Living Room 2"],
        "mode": "vacuum",
        "coverage": "standard",
        "ordered": False,
        "activity": "cleaning",
    }


async def test_clean_whole_floor_mop_heavy_ordered(service, robot):
    result = await service.clean(whole_floor=True, mode="mop", coverage="heavy_duty", ordered=True)
    [call] = [c for c in robot.calls if c[0] == "start_coverage"]
    assert len(call[1]) == len(robot.plan.rooms)
    assert call[2] is CleaningMode.MOP and call[3] is CoverageSetting.HEAVY_DUTY and call[4] is True
    assert result["ordered"] is True


async def test_clean_uses_aliases(service, robot):
    await service.name_room("study", ["office"])
    result = await service.clean(rooms=["Study", "kitchen"], mode="both")
    assert result["rooms"] == ["Office", "Kitchen 1", "Kitchen 2"]
    assert robot.calls[-1][2] is CleaningMode.BOTH


async def test_clean_reports_idle_activity_after_settle_timeout(service, robot):
    async def start_but_stay_docked(*args):
        robot.calls.append(("start_coverage",))

    robot.start_coverage = start_but_stay_docked
    result = await service.clean(rooms=["Office"])
    assert result["accepted"] is True and result["activity"] == "docked"


# ---------------------------------------------------------------- history and bag


async def test_history_newest_first_and_ingested(service, robot):
    robot.records = [
        make_record(1, "2026-10-10T09:00:00+00:00", duration=895, vacuum=304, mop=385, rooms=("Kitchen 2", "Kitchen 1")),
        make_record(2, "2026-10-12T09:00:00+00:00"),
        make_record(3, "2026-10-11T09:00:00+00:00", mop=None),
    ]
    history = await service.history(limit=2)
    sessions = history["sessions"]
    assert [s["started_at"] for s in sessions] == ["2026-10-12T09:00:00Z", "2026-10-11T09:00:00Z"]
    assert sessions[1]["modes"] == ["vacuum"] and sessions[1]["mop_seconds"] == 0
    assert len(service.ledger.sessions()) == 3
    kitchen = next(s for s in service.ledger.sessions() if s["started_at"] == "2026-10-10T09:00:00Z")
    assert kitchen["key"] == (1).to_bytes(22, "big").hex()
    assert (kitchen["duration_seconds"], kitchen["vacuum_seconds"], kitchen["mop_seconds"]) == (895, 304, 384)


async def test_history_keeps_runs_a_cut_short_read_missed(service, robot):
    robot.records = [make_record(i, f"2026-10-1{i}T09:00:00+00:00") for i in range(1, 4)]
    assert len((await service.history(limit=10))["sessions"]) == 3
    robot.records = robot.records[:1]  # a later read stopped early (or the robot forgot runs)
    sessions = (await service.history(limit=10))["sessions"]
    assert [s["started_at"] for s in sessions] == [
        "2026-10-13T09:00:00Z", "2026-10-12T09:00:00Z", "2026-10-11T09:00:00Z"
    ]


async def test_history_without_a_usable_ledger_shows_the_robot_list(service, robot, monkeypatch):
    robot.records = [make_record(1, "2026-10-11T09:00:00+00:00")]

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.ledger, "ingest", locked)
    assert len((await service.history())["sessions"]) == 1


@pytest.mark.parametrize("limit", [0, -1, "x", True])
async def test_history_rejects_bad_limit(service, limit):
    with pytest.raises(MaticDeskError) as caught:
        await service.history(limit=limit)
    assert caught.value.type == "invalid_request"


async def test_bag_report_refreshes_history_and_flags(service, robot):
    robot.records = [make_record(1, "2026-10-13T09:00:00+00:00", vacuum=1200)]
    robot.state = make_state(**CLEANING, bag_full=False)
    report = await service.record_bag_swap(at="2026-10-13T08:00:00+00:00", note="new box")
    assert report["swapped_at"] == "2026-10-13T08:00:00Z"
    assert report["runs_since"] == 1 and report["vacuum_seconds_since"] == 1200
    assert report["in_progress"] is True
    assert report["complete"] is False  # ledger started after the swap and holds no older run


async def test_bag_report_survives_a_locked_ledger(service, robot, monkeypatch):
    service.ledger.record_swap(datetime(2026, 10, 12, tzinfo=UTC), None, NOW)
    robot.records = [make_record(1, "2026-10-13T09:00:00+00:00")]

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.ledger, "ingest", locked)
    report = await service.bag_report()
    assert report["swapped_at"] == "2026-10-12T00:00:00Z" and report["runs_since"] == 0


async def test_bag_report_imports_the_skill_swap_date_once(service, monkeypatch, tmp_path):
    config = tmp_path / "robot.json"
    config.write_text(json.dumps({"bag_swapped_at": "2026-10-06T12:00:00+00:00"}))
    monkeypatch.setenv("MATIC_CONFIG", str(config))
    assert (await service.bag_report())["swapped_at"] == "2026-10-06T12:00:00Z"
    await service.record_bag_swap(at="2026-10-05T12:00:00+00:00")
    assert (await service.bag_report())["swapped_at"] == "2026-10-05T12:00:00Z"


async def test_bag_report_when_offline_uses_ledger(service, robot):
    robot.offline = True
    report = await service.bag_report()
    assert report["swapped_at"] is None and report["bag_full"] is None


def test_parse_when_date_only_is_local_midnight():
    expected = datetime(2026, 10, 6).astimezone().astimezone(UTC)
    assert parse_when("2026-10-06", NOW) == expected


def test_parse_when_today_means_now():
    today = NOW.astimezone().date().isoformat()
    assert parse_when(today, NOW) == NOW


def test_parse_when_local_time_and_offsets():
    assert parse_when("2026-10-06T18:30", NOW) == datetime(2026, 10, 6, 18, 30).astimezone().astimezone(UTC)
    assert parse_when("2026-10-06T18:30:00-04:00", NOW) == datetime(2026, 10, 6, 22, 30, tzinfo=UTC)
    assert parse_when("2026-10-06T18:30:00Z", NOW) == datetime(2026, 10, 6, 18, 30, tzinfo=UTC)
    assert parse_when(None, NOW) == NOW


@pytest.mark.parametrize("value", ["2026-10-16", "2026-10-14T14:02:00+00:00", "last tuesday", "06/10/2026"])
async def test_record_bag_swap_rejects_future_and_garbage(service, value):
    with pytest.raises(MaticDeskError) as caught:
        await service.record_bag_swap(at=value)
    assert caught.value.type == "invalid_request"


async def test_record_bag_swap_allows_up_to_a_minute_ahead(service):
    report = await service.record_bag_swap(at="2026-10-14T14:00:30+00:00")
    assert report["swapped_at"] == "2026-10-14T14:00:30Z"


# ---------------------------------------------------------------- commands and settings


async def test_command_dock(service, robot):
    assert await service.command("dock") == {"sent": "dock", "activity": "returning"}
    assert ("send_command", UserCommand.DOCK) in robot.calls


async def test_command_rejects_unknown(service, robot):
    with pytest.raises(MaticDeskError) as caught:
        await service.command("dance")
    assert caught.value.type == "invalid_request"


async def test_settings_rounds_water_flow(service):
    assert await service.settings() == {
        "water_flow": 1.0, "deep_mop": True, "pet_waste": False, "child_lock": False, "voice": True
    }


@pytest.mark.parametrize(
    "setting, value, call",
    [
        ("water_flow", "1.2", ("set_water_flow", 1.2)),
        ("water-flow", 2, ("set_water_flow", 2.0)),
        ("deep_mop", "off", ("set_deep_mop", False)),
        ("child_lock", True, ("set_binary_setting", "child_lock", True)),
        ("voice", "ON", ("set_binary_setting", "voice", True)),
    ],
)
async def test_set_setting(service, robot, setting, value, call):
    settings = await service.set_setting(setting, value)
    assert robot.calls == [call]
    assert set(settings) == {"water_flow", "deep_mop", "pet_waste", "child_lock", "voice"}


@pytest.mark.parametrize(
    "setting, value",
    [("water_flow", 2.5), ("water_flow", 1.25), ("water_flow", "lots"), ("voice", "maybe"), ("turbo", True)],
)
async def test_set_setting_rejects_bad_values(service, robot, setting, value):
    with pytest.raises(MaticDeskError) as caught:
        await service.set_setting(setting, value)
    assert caught.value.type == "invalid_request"
    assert robot.calls == []


# ---------------------------------------------------------------- rooms, aliases, map


async def test_rooms_payload(service):
    rooms = await service.rooms()
    assert rooms["floor"] is None
    assert rooms["rooms"][0] == {"name": "Bedroom"}
    assert rooms["groups"] == {"Kitchen": ["Kitchen 1", "Kitchen 2"], "Living Room": ["Living Room 1", "Living Room 2"]}
    assert rooms["aliases"] == {}


async def test_name_and_remove_alias(service, tmp_path):
    rooms = await service.name_room("  Work  Space ", ["office", "hallway"])
    assert rooms["aliases"] == {"Work Space": ["Office", "Hallway"]}
    assert (tmp_path / "aliases.json").exists()
    rooms = await service.name_room("work space", ["Bedroom"])
    assert rooms["aliases"] == {"work space": ["Bedroom"]}
    rooms = await service.remove_room_alias("WORK SPACE")
    assert rooms["aliases"] == {}
    with pytest.raises(MaticDeskError) as caught:
        await service.remove_room_alias("nope")
    assert caught.value.type == "invalid_request"


@pytest.mark.parametrize("alias", ["Kitchen 1", "kitchen", "Living Rooms", "all", "Whole  House"])
async def test_name_room_rejects_aliases_that_shadow_names(service, alias, tmp_path):
    with pytest.raises(MaticDeskError) as caught:
        await service.name_room(alias, ["Office"])
    assert caught.value.type == "invalid_request"
    assert not (tmp_path / "aliases.json").exists()


async def test_map_png_draws_route_only_on_a_mission(service, robot):
    png = await service.map_png()
    assert png.startswith(b"\x89PNG") and ("get_route",) not in robot.calls
    robot.state = make_state(**CLEANING)
    png = await service.map_png(route=True)
    assert png.startswith(b"\x89PNG") and ("get_route",) in robot.calls


async def test_close_closes_robot(service, robot):
    await service.status()
    await service.close()
    assert robot.closed is True


async def test_robot_rebuilt_when_robot_json_changes(tmp_path, monkeypatch):
    config = tmp_path / "robot.json"
    config.write_text("{}")
    monkeypatch.setenv("MATIC_CONFIG", str(config))
    robots: list[FakeRobot] = []

    def factory():
        robots.append(FakeRobot())
        return robots[-1]

    service = MaticService(robot_factory=factory, ledger=Ledger(":memory:"))
    await service.status()
    await service.status()
    assert len(robots) == 1
    config.write_text('{"credential": "new"}')  # `matic-desk pair` ran again
    os.utime(config, ns=(1, 1))
    await service.status()
    assert len(robots) == 2 and robots[0].closed is True


async def test_robot_factory_called_lazily_and_retried(tmp_path):
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise MaticDeskError("not_paired", "pair first")
        return FakeRobot()

    service = MaticService(robot_factory=factory, ledger=Ledger(":memory:"))
    assert attempts == []
    assert (await service.status())["activity"] == "offline"
    assert (await service.status())["activity"] == "docked"
