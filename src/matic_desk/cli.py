"""`matic-desk` command line: concise text by default, contract JSON with --json."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__, text
from .config import setup_logging
from .errors import MaticDeskError
from .service import COVERAGES, MODES, SETTINGS, MaticService


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print contract JSON")

    parser = argparse.ArgumentParser(
        prog="matic-desk",
        description="Read and control a Matic robot vacuum on your local network (unofficial).",
    )
    # Its own action: `parents` shares action objects, so a shared default would leak.
    parser.add_argument("--json", action="store_true", help="print contract JSON")
    parser.add_argument("--version", action="version", version=f"matic-desk {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, description=help_text, parents=[common])

    add("status", "activity, battery, room, errors and bag flags")
    add("settings", "water flow, deep mop, pet waste avoidance, child lock, voice")
    add("rooms", "rooms, groups and aliases")
    p = add("history", "recent cleaning runs")
    p.add_argument("-n", "--limit", type=int, default=10, help="number of runs (default 10)")

    p = add("bag", "dust bag usage since the last recorded swap")
    bag_sub = p.add_subparsers(dest="bag_command", metavar="swap")
    swap = bag_sub.add_parser("swap", help="record a bag swap", parents=[common])
    swap.add_argument("--at", help="when: YYYY-MM-DD, YYYY-MM-DDTHH:MM or ISO 8601 (local time; default now)")
    swap.add_argument("--note", help="optional note")

    p = add("clean", "start cleaning rooms (names, groups or aliases)")
    p.add_argument("rooms", nargs="*", metavar="ROOM")
    p.add_argument("--all", dest="whole_floor", action="store_true", help="clean the whole floor")
    p.add_argument("--mode", choices=list(MODES), default="both")
    p.add_argument("--coverage", choices=list(COVERAGES), default="standard")
    p.add_argument("--ordered", action="store_true", help="clean rooms in the order given")

    for name, help_text in (
        ("pause", "pause the current run"),
        ("resume", "resume a paused run"),
        ("stop", "stop the current run"),
        ("dock", "send the robot home"),
    ):
        add(name, help_text)

    p = add("set", "change a setting (on/off, or 0.5-2.0 for water_flow)")
    p.add_argument("setting", type=lambda s: s.replace("-", "_"), choices=list(SETTINGS))
    p.add_argument("value")

    p = add("map", "save a PNG of the floor plan")
    p.add_argument("--out", help="output file (default: matic-map.png in the temp folder; the path is printed)")
    p.add_argument("--no-route", dest="route", action="store_false", help="skip the current route")

    p = add("alias", "name one or more rooms, e.g. `alias office Room 3`")
    p.add_argument("name")
    p.add_argument("rooms", nargs="+", metavar="ROOM")
    p = add("unalias", "remove a room alias")
    p.add_argument("name")

    add("pair", "one-time Bluetooth pairing (run in Terminal)")
    return parser


def _emit(args: argparse.Namespace, data: Any, human: str) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else human)


async def run(args: argparse.Namespace, service: MaticService) -> int:
    command = args.command
    if command == "status":
        status = await service.status()
        _emit(args, status, text.status_line(status))
    elif command == "settings":
        settings = await service.settings()
        _emit(args, settings, text.settings_line(settings))
    elif command == "rooms":
        rooms = await service.rooms()
        lines = [f"Floor: {rooms['floor'] or 'default'}", "Rooms: " + ", ".join(r["name"] for r in rooms["rooms"])]
        if rooms["groups"]:
            lines.append("Groups: " + "; ".join(f"{b} = {' + '.join(m)}" for b, m in rooms["groups"].items()))
        if rooms["aliases"]:
            lines.append("Aliases: " + "; ".join(f"{a} = {' + '.join(r)}" for a, r in rooms["aliases"].items()))
        _emit(args, rooms, "\n".join(lines))
    elif command == "history":
        history = await service.history(args.limit)
        lines = [text.session_line(s) for s in history["sessions"]]
        _emit(args, history, "\n".join(lines) or "No cleaning runs on the robot.")
    elif command == "bag" and args.bag_command == "swap":
        report = await service.record_bag_swap(at=args.at, note=args.note)
        _emit(args, report, "Recorded a bag swap.\n" + "\n".join(text.bag_lines(report)))
    elif command == "bag":
        report = await service.bag_report()
        _emit(args, report, "\n".join(text.bag_lines(report)))
    elif command == "clean":
        result = await service.clean(
            rooms=args.rooms, whole_floor=args.whole_floor, mode=args.mode,
            coverage=args.coverage, ordered=args.ordered,
        )
        _emit(args, result, text.clean_line(result))
    elif command in ("pause", "resume", "stop", "dock"):
        result = await service.command(command)
        _emit(args, result, f"Sent {result['sent']}; robot is now {result['activity']}.")
    elif command == "set":
        settings = await service.set_setting(args.setting, args.value)
        _emit(args, settings, f"{args.setting} updated. {text.settings_line(settings)}")
    elif command == "map":
        png = await service.map_png(route=args.route)
        # Not the current directory by default: it is often a git checkout, and
        # the map shows the home's layout.
        out = Path(args.out).expanduser() if args.out else Path(tempfile.gettempdir()) / "matic-map.png"
        out.write_bytes(png)
        _emit(args, {"path": str(out), "bytes": len(png)}, f"Map written to {out}")
    elif command == "alias":
        rooms = await service.name_room(args.name, args.rooms)
        targets = rooms["aliases"].get(" ".join(args.name.split()), [])
        _emit(args, rooms, f"'{args.name}' now means {', '.join(targets)}.")
    elif command == "unalias":
        rooms = await service.remove_room_alias(args.name)
        _emit(args, rooms, f"Removed alias '{args.name}'.")
    else:  # pragma: no cover - argparse rejects anything else
        raise MaticDeskError("invalid_request", f"Unknown command {command!r}.")
    return 0


async def _main(args: argparse.Namespace) -> int:
    if args.command == "pair":
        from .pairing import pair

        await pair()
        return 0
    service = MaticService()
    try:
        return await run(args, service)
    finally:
        await service.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    setup_logging(to_file=False, level=logging.WARNING)
    try:
        if args.command == "pair":
            from .pairing import reexec_through_bluetooth_shim

            reexec_through_bluetooth_shim(argv)  # on macOS this restarts inside the Bluetooth helper
        return asyncio.run(_main(args))
    except MaticDeskError as err:
        if args.json:
            print(json.dumps({"error": err.to_dict()}, indent=2))
        else:
            print(f"Error ({err.type}): {err.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
