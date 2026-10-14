import asyncio
import base64
import io
import json

import pytest
from conftest import CLEANING, make_state

from matic_desk import bridge
from matic_desk.bridge import Bridge, Output, dispatch, serve


def lines_of(sink: io.BytesIO) -> list[dict]:
    return [json.loads(line) for line in sink.getvalue().splitlines()]


async def wait_until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_ping(service):
    assert await dispatch(service, {"id": 1, "method": "ping"}) == {"id": 1, "ok": True, "result": {"pong": True}}


async def test_unknown_method_is_invalid_request(service):
    response = await dispatch(service, {"id": "x", "method": "nope", "params": {}})
    assert response["id"] == "x" and response["ok"] is False
    assert response["error"]["type"] == "invalid_request"


@pytest.mark.parametrize(
    "request_",
    [
        {"id": 1, "method": "status", "params": {"verbose": True}},
        {"id": 1, "method": "command", "params": {}},
        {"id": 1, "method": "status", "params": [1]},
        ["not", "an", "object"],
    ],
)
async def test_bad_requests(service, request_):
    response = await dispatch(service, request_)
    assert response["ok"] is False and response["error"]["type"] == "invalid_request"


async def test_service_errors_become_error_envelopes(service):
    response = await dispatch(service, {"id": 4, "method": "clean", "params": {"rooms": ["Garage"], "mode": "vacuum"}})
    assert response == {"id": 4, "ok": False, "error": {"type": "room_not_found", "message": response["error"]["message"]}}


async def test_unexpected_exceptions_are_contained(service, robot):
    async def broken():
        raise RuntimeError("boom")

    robot.get_floor_plan = broken
    response = await dispatch(service, {"id": 5, "method": "rooms"})
    assert response["ok"] is False and response["error"]["type"] == "robot_error"


async def test_methods_return_contract_results(service):
    calls = [
        ("status", {}, "activity"),
        ("settings", {}, "water_flow"),
        ("rooms", {}, "groups"),
        ("history", {"limit": 3}, "sessions"),
        ("bag", {}, "estimate"),
        ("record_bag_swap", {"at": "2026-10-10", "note": "n"}, "swapped_at"),
        ("clean", {"rooms": ["Office"], "whole_floor": False, "mode": "vacuum", "coverage": "standard", "ordered": False}, "accepted"),
        ("command", {"name": "pause"}, "sent"),
        ("set_setting", {"setting": "voice", "value": False}, "voice"),
        ("name_room", {"alias": "study", "rooms": ["Office"]}, "aliases"),
        ("remove_room_alias", {"alias": "study"}, "aliases"),
    ]
    for index, (method, params, key) in enumerate(calls):
        response = await dispatch(service, {"id": index, "method": method, "params": params})
        assert response["ok"] is True, (method, response)
        assert key in response["result"]


async def test_map_is_base64_png(service):
    response = await dispatch(service, {"id": 9, "method": "map", "params": {"route": False}})
    assert base64.b64decode(response["result"]["png_base64"]).startswith(b"\x89PNG")


async def test_serve_answers_each_line_and_exits_on_eof(service, robot):
    reader = asyncio.StreamReader()
    for line in (b'{"id":1,"method":"ping"}\n', b"not json\n", b"\n", b'{"id":2,"method":"status"}\n',
                 b'{"id":3,"method":"nope"}\n'):
        reader.feed_data(line)
    reader.feed_eof()
    sink = io.BytesIO()
    await asyncio.wait_for(serve(service, reader, Output(sink), background=False), 5)
    by_id = {line["id"]: line for line in lines_of(sink)}
    assert by_id[1]["result"] == {"pong": True}
    assert by_id[None]["error"]["type"] == "invalid_request"
    assert by_id[2]["ok"] is True and by_id[2]["result"]["activity"] == "docked"
    assert by_id[3]["error"]["type"] == "invalid_request"
    assert robot.closed is True


async def test_requests_run_concurrently(service, robot):
    gate = asyncio.Event()
    original = robot.get_floor_plan

    async def slow_plan():
        await gate.wait()
        return await original()

    robot.get_floor_plan = slow_plan
    reader = asyncio.StreamReader()
    sink = io.BytesIO()
    task = asyncio.create_task(serve(service, reader, Output(sink), background=False))
    reader.feed_data(b'{"id":1,"method":"rooms"}\n{"id":2,"method":"ping"}\n')
    await wait_until(lambda: any(line.get("id") == 2 for line in lines_of(sink)))
    assert not any(line.get("id") == 1 for line in lines_of(sink))
    gate.set()
    reader.feed_eof()
    await asyncio.wait_for(task, 5)
    assert [line["id"] for line in lines_of(sink)] == [2, 1]


async def test_publish_status_dedups_and_flags_run_end(service):
    sink = io.BytesIO()
    hub = Bridge(service, Output(sink))
    cleaning = {"connected": True, "activity": "cleaning", "battery": 90, "current_room": "Office",
                "errors": [], "bag_full": False, "bag_missing": False, "updated_at": "a"}
    await hub.publish_status(cleaning)
    await hub.publish_status({**cleaning, "updated_at": "b"})
    assert len(lines_of(sink)) == 1 and not hub._ingest_now.is_set()
    await hub.publish_status({**cleaning, "activity": "returning"})
    assert len(lines_of(sink)) == 2 and hub._ingest_now.is_set()


async def test_background_status_and_bag_events(service, robot, monkeypatch):
    monkeypatch.setattr(bridge, "RECONNECT_SECONDS", 0.01)
    robot.stream = [make_state(**CLEANING), make_state(**CLEANING), make_state()]
    reader = asyncio.StreamReader()
    sink = io.BytesIO()
    task = asyncio.create_task(serve(service, reader, Output(sink)))

    def events(kind: str) -> list[dict]:
        return [line["data"] for line in lines_of(sink) if line.get("event") == kind]

    await wait_until(lambda: events("bag") and [s["activity"] for s in events("status")][-1:] == ["docked"]
                     and any(s["activity"] == "cleaning" for s in events("status")))
    reader.feed_eof()
    await asyncio.wait_for(task, 5)
    activities = [s["activity"] for s in events("status")]
    assert all(a != b for a, b in zip(activities, activities[1:]))  # only changes are sent
    assert "estimate" in events("bag")[0]
    assert robot.closed is True
