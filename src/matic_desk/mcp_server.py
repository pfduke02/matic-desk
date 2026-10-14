"""MCP server (stdio) so Claude, Codex and other MCP clients can drive the robot.

Each tool returns the contract object as structured content, plus a one-line
summary and the JSON as text for clients that only read text. Nothing here
writes to stdout except the SDK's own transport.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import text
from .config import setup_logging
from .errors import MaticDeskError
from .service import MaticService

log = logging.getLogger(__name__)

TOOL_NAMES = (
    "matic_status",
    "matic_rooms",
    "matic_bag_report",
    "matic_history",
    "matic_map",
    "matic_start_cleaning",
    "matic_pause",
    "matic_resume",
    "matic_stop",
    "matic_dock",
    "matic_set_setting",
    "matic_record_bag_swap",
    "matic_name_room",
)

INSTRUCTIONS = """\
Controls one Matic robot vacuum/mop over the local network (no cloud).
Use matic_rooms to learn room names, groups (e.g. "Kitchen" covers "Kitchen 1"
and "Kitchen 2") and the user's aliases before cleaning if unsure. Choose the
cleaning mode from the user's words: "vacuum" when they say vacuum, "mop" when
they say mop, "both" when they just say clean. Cleaning, pausing, stopping and
docking move the real robot immediately.
"""

ROOMS_HELP = (
    "Room names from matic_rooms (e.g. 'Kitchen 1'), group names that cover numbered "
    "segments (e.g. 'Kitchen', 'Living Room'), or the user's aliases. Case-insensitive."
)


def _read() -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)


def _write(title: str, *, idempotent: bool, destructive: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def result(data: dict[str, Any], summary: str) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(type="text", text=summary),
            TextContent(type="text", text=json.dumps(data, ensure_ascii=False)),
        ],
        structuredContent=data,
    )


def build_server(service: MaticService | None = None) -> FastMCP:
    service = service or MaticService()

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await service.close()

    mcp = FastMCP("matic-desk", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool(
        name="matic_status",
        title="Matic status",
        description=(
            "Current robot status: activity (cleaning, paused, returning, charging, docked, ready, "
            "error or offline), battery %, current room, error codes, bag full/missing flags and "
            "firmware, plus settings (water flow, deep mop, pet waste avoidance, child lock, voice). "
            "Never fails for connectivity: an unreachable robot reports activity 'offline' with a message."
        ),
        annotations=_read(),
    )
    async def matic_status() -> CallToolResult:
        status = await service.status()
        settings = None
        if status["connected"]:
            try:
                settings = await service.settings()
            except MaticDeskError as err:
                log.info("Settings unavailable: %s", err)
        line = text.status_line(status)
        if settings:
            line += " · " + text.settings_line(settings)
        return result({"status": status, "settings": settings}, line)

    @mcp.tool(
        name="matic_rooms",
        title="Matic rooms",
        description=(
            "Rooms on the robot's map, groups of numbered segments (e.g. 'Living Room' covers "
            "'Living Room 1..3') and the user's room aliases. Use these names with matic_start_cleaning."
        ),
        annotations=_read(),
    )
    async def matic_rooms() -> CallToolResult:
        rooms = await service.rooms()
        return result(rooms, text.rooms_line(rooms))

    @mcp.tool(
        name="matic_bag_report",
        title="Matic dust bag report",
        description=(
            "Dust bag usage since the last recorded bag swap: swap date, days, runs, total cleaning, "
            "vacuum and mop seconds, whether the robot reports the bag full or missing, previous bags "
            "and an estimate of the remaining life. The robot has no fill gauge, only full/missing flags. "
            "If 'complete' is false, runs before 'counted_from' may be missing; say so."
        ),
        annotations=_read(),
    )
    async def matic_bag_report() -> CallToolResult:
        report = await service.bag_report()
        return result(report, " ".join(text.bag_lines(report)))

    @mcp.tool(
        name="matic_history",
        title="Matic cleaning history",
        description=(
            "Recent cleaning runs, newest first: start and end (UTC), total duration, vacuum and mop "
            "seconds, rooms, modes and whether the run completed. The robot keeps about 20 runs; "
            "older ones come from the local ledger."
        ),
        annotations=_read(),
    )
    async def matic_history(
        limit: Annotated[int, Field(ge=1, le=100, description="How many runs to return.")] = 10,
    ) -> CallToolResult:
        history = await service.history(limit)
        sessions = history["sessions"]
        if sessions:
            latest = sessions[0]
            line = (
                f"{len(sessions)} recent runs; latest {text.local_time(latest['started_at'])}: "
                f"{', '.join(latest['rooms']) or '?'} ({text.duration(latest['duration_seconds'])})."
            )
        else:
            line = "No cleaning runs on the robot."
        return result(history, line)

    @mcp.tool(
        name="matic_map",
        title="Matic map",
        description=(
            "A 1024x1024 PNG of the floor plan with room names, the robot's position and, while a run "
            "is active, its route."
        ),
        annotations=_read(),
        structured_output=False,
    )
    async def matic_map(
        route: Annotated[bool, Field(description="Draw the current route when a run is active.")] = True,
    ) -> Image:
        return Image(data=await service.map_png(route=route), format="png")

    @mcp.tool(
        name="matic_start_cleaning",
        title="Start cleaning",
        description=(
            "Start the robot cleaning now. Give `rooms`, or set `whole_floor` for everything.\n"
            "Pick `mode` from the user's words: 'vacuum' when they say vacuum (or hoover/sweep), "
            "'mop' when they say mop, 'both' (vacuum then mop) when they just say clean or ask for both.\n"
            "`coverage`: 'standard' unless they ask for a quick pass ('quick') or a deep, thorough "
            "clean ('heavy_duty').\n"
            "`ordered`: true only if they want the rooms done in the order given.\n"
            "Returns the rooms accepted and the robot's activity a few seconds later."
        ),
        annotations=_write("Start cleaning", idempotent=False),
    )
    async def matic_start_cleaning(
        rooms: Annotated[list[str] | None, Field(description=ROOMS_HELP)] = None,
        whole_floor: Annotated[bool, Field(description="Clean every room on the map.")] = False,
        mode: Annotated[Literal["vacuum", "mop", "both"], Field(description="vacuum, mop, or both.")] = "both",
        coverage: Annotated[
            Literal["quick", "standard", "heavy_duty"], Field(description="Cleaning intensity.")
        ] = "standard",
        ordered: Annotated[bool, Field(description="Clean rooms in the order given.")] = False,
    ) -> CallToolResult:
        cleaning = await service.clean(
            rooms=rooms, whole_floor=whole_floor, mode=mode, coverage=coverage, ordered=ordered
        )
        return result(cleaning, text.clean_line(cleaning))

    async def _command(name: str) -> CallToolResult:
        sent = await service.command(name)
        return result(sent, f"Sent {sent['sent']}; robot is now {sent['activity']}.")

    @mcp.tool(
        name="matic_pause",
        title="Pause",
        description="Pause the current cleaning run (resume later with matic_resume).",
        annotations=_write("Pause", idempotent=True),
    )
    async def matic_pause() -> CallToolResult:
        return await _command("pause")

    @mcp.tool(
        name="matic_resume",
        title="Resume",
        description="Resume a paused cleaning run.",
        annotations=_write("Resume", idempotent=True),
    )
    async def matic_resume() -> CallToolResult:
        return await _command("resume")

    @mcp.tool(
        name="matic_stop",
        title="Stop",
        description="Stop and end the current cleaning run (the robot stays where it is). Use matic_dock to send it home.",
        annotations=_write("Stop", idempotent=True, destructive=True),
    )
    async def matic_stop() -> CallToolResult:
        return await _command("stop")

    @mcp.tool(
        name="matic_dock",
        title="Send home",
        description="Send the robot back to its dock (ends the current run).",
        annotations=_write("Send home", idempotent=True),
    )
    async def matic_dock() -> CallToolResult:
        return await _command("dock")

    @mcp.tool(
        name="matic_set_setting",
        title="Change a setting",
        description=(
            "Change one robot setting and return all settings. water_flow: mop water amount, 0.5 to 2.0 "
            "in 0.1 steps (1.0 is normal). deep_mop: double-pass mopping, on/off. pet_waste: pet waste "
            "avoidance, on/off. child_lock: on/off. voice: spoken prompts, on/off."
        ),
        annotations=_write("Change a setting", idempotent=True),
    )
    async def matic_set_setting(
        setting: Literal["water_flow", "deep_mop", "pet_waste", "child_lock", "voice"],
        value: Annotated[
            bool | float | str, Field(description="A number for water_flow; true/false or 'on'/'off' otherwise.")
        ],
    ) -> CallToolResult:
        settings = await service.set_setting(setting, value)
        return result(settings, f"{setting} updated. Settings: {text.settings_line(settings)}.")

    @mcp.tool(
        name="matic_record_bag_swap",
        title="Record a bag swap",
        description=(
            "Record that the user changed the robot's dust bag, then return the bag report. Omit `when` "
            "if they changed it today or just now. For an earlier day pass that local date or time, "
            "e.g. '2026-10-06' or '2026-10-06T18:30' (convert 'yesterday' or 'last Monday' to a date "
            "yourself). A date earlier than the current bag's swap corrects it (the later swap is "
            "removed), so use this to fix a wrong date too."
        ),
        annotations=_write("Record a bag swap", idempotent=False),
    )
    async def matic_record_bag_swap(
        when: Annotated[
            str | None, Field(description="YYYY-MM-DD, YYYY-MM-DDTHH:MM or ISO 8601; omit for today/now.")
        ] = None,
        note: Annotated[str | None, Field(description="Optional note, e.g. 'new box of bags'.")] = None,
    ) -> CallToolResult:
        report = await service.record_bag_swap(at=when, note=note)
        return result(report, f"Recorded a bag swap at {text.local_time(report['swapped_at'])}. "
                      + (report["estimate"]["text"] or ""))

    @mcp.tool(
        name="matic_name_room",
        title="Name a room",
        description=(
            "Create or replace a room alias so the user can say e.g. 'office' for 'Room 3' or 'downstairs' "
            "for several rooms. An alias can't reuse a room name, a group name or 'all'. Returns the "
            "updated rooms, groups and aliases."
        ),
        annotations=_write("Name a room", idempotent=True),
    )
    async def matic_name_room(
        alias: Annotated[str, Field(description="The new name, e.g. 'office'.")],
        rooms: Annotated[list[str], Field(description=ROOMS_HELP)],
    ) -> CallToolResult:
        updated = await service.name_room(alias, rooms)
        targets = updated["aliases"].get(" ".join(alias.split()), [])
        return result(updated, f"'{alias}' now means {', '.join(targets)}.")

    return mcp


def main() -> None:
    setup_logging()
    log.info("matic-desk-mcp starting")
    build_server().run("stdio")


if __name__ == "__main__":
    main()
