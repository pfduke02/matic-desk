#!/usr/bin/env python3
"""Fake matic-desk-bridge for UI development. Stdlib only; never touches a robot.

Speaks the JSON-lines bridge protocol from docs/protocol.md with canned,
contract-shaped data. Run the app against it with a wrapper script:

    #!/bin/sh
    exec python3 /path/to/app/dev/fake_bridge.py
    MATIC_DESK_BACKEND=/path/to/wrapper "Matic Desk.app/Contents/MacOS/MaticDesk"

Env: FAKE_BRIDGE_OFFLINE=1 reports an offline robot; FAKE_BRIDGE_NO_SWAP=1
starts with no recorded bag swap.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

OUT_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()

# Invented placeholder map (same as tests/conftest.py); not anyone's real home.
ROOMS = ["Kitchen 1", "Kitchen 2", "Living Room 1", "Living Room 2", "Office", "Hallway", "Bedroom"]
GROUPS = {"Kitchen": ["Kitchen 1", "Kitchen 2"], "Living Room": ["Living Room 1", "Living Room 2"]}
# 1x1 transparent PNG.
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

state = {
    "activity": "docked",
    "battery": 100,
    "room": None,
    "swapped_at": None if os.environ.get("FAKE_BRIDGE_NO_SWAP") == "1"
    else datetime.now(timezone.utc) - timedelta(days=8, hours=2),
    "aliases": {"study": ["Office"]},
    "settings": {"water_flow": 1.0, "deep_mop": True, "pet_waste": False, "child_lock": False, "voice": True},
}
OFFLINE = os.environ.get("FAKE_BRIDGE_OFFLINE") == "1"


class BridgeError(Exception):
    def __init__(self, type_: str, message: str) -> None:
        super().__init__(message)
        self.type = type_
        self.message = message


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def log(msg: str) -> None:
    with OUT_LOCK:
        sys.stderr.write(f"fake_bridge: {msg}\n")
        sys.stderr.flush()


def emit(obj: dict) -> None:
    with OUT_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def status() -> dict:
    now = iso(datetime.now(timezone.utc))
    if OFFLINE:
        return {
            "connected": False, "robot_name": None, "activity": "offline", "battery": None,
            "charging": None, "low_charge": None, "current_room": None, "errors": [],
            "bag_full": None, "bag_missing": None, "firmware": None, "updated_at": now,
            "message": "Robot unreachable on the local network (fake).",
        }
    a = state["activity"]
    return {
        "connected": True, "robot_name": "matic-abcd-efgh", "activity": a,
        "battery": state["battery"], "charging": a in ("charging", "docked"), "low_charge": False,
        "current_room": state["room"], "errors": [], "bag_full": False, "bag_missing": False,
        "firmware": "v0.0-fake", "updated_at": now, "message": None,
    }


def rooms() -> dict:
    return {"floor": None, "rooms": [{"name": n} for n in ROOMS], "groups": GROUPS,
            "aliases": state["aliases"]}


def bag() -> dict:
    swapped = state["swapped_at"]
    if swapped is None:
        return {
            "swapped_at": None, "days_since": None, "runs_since": None,
            "cleaning_seconds_since": None, "vacuum_seconds_since": None, "mop_seconds_since": None,
            "counted_from": None, "complete": False, "in_progress": state["activity"] == "cleaning",
            "bag_full": False, "bag_missing": False, "last_bag_full_at": None, "previous_bags": [],
            "estimate": {"basis": "unknown", "days_left": None, "vacuum_hours_left": None,
                         "text": "Record a bag swap to start tracking this bag (estimate)."},
        }
    days = (datetime.now(timezone.utc) - swapped).total_seconds() / 86400
    fresh = days < 1
    return {
        "swapped_at": iso(swapped), "days_since": round(days, 1), "runs_since": 0 if fresh else 15,
        "cleaning_seconds_since": 0 if fresh else 50880,
        "vacuum_seconds_since": 0 if fresh else 34800,
        "mop_seconds_since": 0 if fresh else 14300,
        "counted_from": iso(swapped + timedelta(hours=2)), "complete": fresh,
        "in_progress": state["activity"] == "cleaning", "bag_full": False, "bag_missing": False,
        "last_bag_full_at": None, "previous_bags": [],
        "estimate": {"basis": "guidance", "days_left": max(0.0, round(7 - days, 1)),
                     "vacuum_hours_left": None,
                     "text": f"Estimate: Matic suggests about 7 days per bag; this bag is on day {int(days) + 1}."},
    }


def resolve(names: list[str]) -> list[str]:
    out: list[str] = []
    for raw in names:
        key = " ".join(raw.split()).lower()
        aliases = {k.lower(): v for k, v in state["aliases"].items()}
        groups = {k.lower(): v for k, v in GROUPS.items()}
        exact = {r.lower(): r for r in ROOMS}
        if key in aliases:
            found = aliases[key]
        elif key in exact:
            found = [exact[key]]
        elif key in groups or key.rstrip("s") in groups:
            found = groups.get(key) or groups[key.rstrip("s")]
        elif key in ("all", "everything", "whole floor", "whole house"):
            found = ROOMS
        else:
            raise BridgeError("room_not_found", f"No room '{raw}'. Rooms: {', '.join(ROOMS)}")
        out += [r for r in found if r not in out]
    return out


def set_activity(activity: str, room: str | None = None) -> None:
    state["activity"] = activity
    state["room"] = room
    emit({"event": "status", "data": status()})


def offline_check() -> None:
    if OFFLINE:
        raise BridgeError("offline", "Robot unreachable on the local network (fake).")


def handle(method: str, params: dict):
    if method == "ping":
        return {"pong": True}
    if method == "status":
        return status()
    if method == "settings":
        offline_check()
        return dict(state["settings"])
    if method == "rooms":
        offline_check()
        return rooms()
    if method == "history":
        return {"sessions": []}
    if method == "bag":
        return bag()
    if method == "record_bag_swap":
        at = params.get("at")
        try:
            when = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
        except ValueError:
            raise BridgeError("invalid_request", f"Bad date: {at}")
        if when.tzinfo is None:
            when = when.astimezone()
        state["swapped_at"] = when.astimezone(timezone.utc)
        report = bag()
        emit({"event": "bag", "data": report})
        return report
    if method == "clean":
        offline_check()
        mode, coverage = params.get("mode", "both"), params.get("coverage", "standard")
        if mode not in ("vacuum", "mop", "both") or coverage not in ("quick", "standard", "heavy_duty"):
            raise BridgeError("invalid_request", "Bad mode or coverage.")
        if params.get("whole_floor"):
            targets = list(ROOMS)
        elif params.get("rooms"):
            targets = resolve(params["rooms"])
        else:
            raise BridgeError("invalid_request", "Pick rooms or whole_floor.")
        time.sleep(0.5)
        set_activity("cleaning", targets[0])
        return {"accepted": True, "rooms": targets, "mode": mode, "coverage": coverage,
                "ordered": bool(params.get("ordered")), "activity": "cleaning"}
    if method == "command":
        offline_check()
        name = params.get("name")
        after = {"pause": "paused", "resume": "cleaning", "stop": "ready", "dock": "returning"}
        if name not in after:
            raise BridgeError("invalid_request", f"Unknown command {name!r}.")
        time.sleep(0.3)
        set_activity(after[name], state["room"] if name in ("pause", "resume") else None)
        return {"sent": name, "activity": after[name]}
    if method == "set_setting":
        offline_check()
        setting = params.get("setting")
        if setting not in state["settings"]:
            raise BridgeError("invalid_request", f"Unknown setting {setting!r}.")
        state["settings"][setting] = params.get("value")
        return dict(state["settings"])
    if method == "map":
        return {"png_base64": PNG_1X1}
    if method == "name_room":
        state["aliases"][params["alias"]] = resolve(params.get("rooms", []))
        return rooms()
    if method == "remove_room_alias":
        state["aliases"].pop(params.get("alias"), None)
        return rooms()
    raise BridgeError("invalid_request", f"Unknown method {method!r}.")


def serve(request: dict) -> None:
    rid, method = request.get("id"), request.get("method", "")
    log(f"<- {method} #{rid}")
    try:
        with STATE_LOCK:
            result = handle(method, request.get("params") or {})
        emit({"id": rid, "ok": True, "result": result})
    except BridgeError as e:
        emit({"id": rid, "ok": False, "error": {"type": e.type, "message": e.message}})
    except Exception as e:  # noqa: BLE001 - a fake should never crash the UI
        emit({"id": rid, "ok": False, "error": {"type": "robot_error", "message": str(e)}})


def main() -> None:
    log("started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log("ignored a non-JSON line")
            continue
        # Requests are handled concurrently, like the real bridge.
        threading.Thread(target=serve, args=(request,), daemon=True).start()
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
