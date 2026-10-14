"""Human-readable renderings of contract objects (CLI output, MCP summaries)."""

from __future__ import annotations

from typing import Any

from .ledger import parse_iso


def hours(seconds: int | float | None) -> str:
    return "?" if seconds is None else f"{seconds / 3600:.1f} h"


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    minutes = int(seconds) // 60
    hrs, minutes = divmod(minutes, 60)
    return f"{hrs}h {minutes:02d}m" if hrs else f"{minutes}m"


def local_time(value: str | None, *, with_time: bool = True) -> str:
    moment = parse_iso(value)
    if moment is None:
        return "?"
    local = moment.astimezone()
    return local.strftime("%a %b %-d %H:%M" if with_time else "%a %b %-d %Y")


def on_off(value: bool | None) -> str:
    return "?" if value is None else ("on" if value else "off")


def bag_flag_text(status_or_report: dict[str, Any]) -> str:
    if status_or_report.get("bag_missing"):
        return "bag MISSING"
    if status_or_report.get("bag_full"):
        return "bag FULL"
    if status_or_report.get("bag_full") is False:
        return "bag ok (no bag error)"
    return "no bag error reported"


def status_line(status: dict[str, Any]) -> str:
    name = status.get("robot_name") or "Matic"
    if not status.get("connected"):
        return f"{name} is offline: {status.get('message') or 'unreachable'}"
    parts = [f"{name} is {status['activity']}" + (f" in {status['current_room']}" if status.get("current_room") else "")]
    if status.get("battery") is not None:
        parts.append(f"battery {status['battery']}%" + (" (charging)" if status.get("charging") else "")
                     + (" (low)" if status.get("low_charge") else ""))
    errors = status.get("errors") or []
    parts.append("errors: " + ", ".join(e["label"] for e in errors) if errors else "no errors")
    parts.append(bag_flag_text(status))
    return " · ".join(parts)


def settings_line(settings: dict[str, Any] | None) -> str:
    if not settings:
        return "settings unavailable"
    flow = settings.get("water_flow")
    return (
        f"water flow {'?' if flow is None else f'{flow:.1f}x'}, "
        f"deep mop {on_off(settings.get('deep_mop'))}, "
        f"pet waste avoidance {on_off(settings.get('pet_waste'))}, "
        f"child lock {on_off(settings.get('child_lock'))}, "
        f"voice {on_off(settings.get('voice'))}"
    )


def rooms_line(rooms: dict[str, Any]) -> str:
    names = [room["name"] for room in rooms.get("rooms") or []]
    text = f"{len(names)} rooms: {', '.join(names) or 'none'}."
    groups = rooms.get("groups") or {}
    if groups:
        text += " Groups: " + ", ".join(f"{base} ({len(members)})" for base, members in groups.items()) + "."
    aliases = rooms.get("aliases") or {}
    if aliases:
        text += " Aliases: " + ", ".join(f"{a} = {' + '.join(r)}" for a, r in aliases.items()) + "."
    return text


def session_line(session: dict[str, Any]) -> str:
    modes = "/".join(session.get("modes") or []) or "?"
    rooms = ", ".join(session.get("rooms") or []) or "?"
    line = (
        f"{local_time(session.get('started_at')):<17} {duration(session.get('duration_seconds')):>7}  "
        f"vac {duration(session.get('vacuum_seconds')):>6}  mop {duration(session.get('mop_seconds')):>6}  "
        f"{modes:<10} {rooms}"
    )
    if session.get("completed") is False:
        line += "  (not completed)"
    return line


def bag_lines(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if report.get("swapped_at"):
        lines.append(
            f"Bag swapped {local_time(report['swapped_at'])} ({report['days_since']:.1f} days ago)."
        )
        lines.append(
            f"Since then: {report['runs_since']} runs · {hours(report['cleaning_seconds_since'])} cleaning"
            f" · {hours(report['vacuum_seconds_since'])} vacuum · {hours(report['mop_seconds_since'])} mop."
        )
        if not report.get("complete"):
            lines.append(
                f"Counted from {local_time(report.get('counted_from'))}; runs before that may be missing."
            )
    else:
        lines.append("No bag swap recorded.")
    if report.get("in_progress"):
        lines.append("A run is in progress; its time is added once it shows up in the history.")
    lines.append("Robot reports: " + bag_flag_text(report)
                 + (f" (last full {local_time(report['last_bag_full_at'])})" if report.get("last_bag_full_at") else "")
                 + ".")
    lines.append((report.get("estimate") or {}).get("text") or "")
    return [line for line in lines if line]


def clean_line(result: dict[str, Any]) -> str:
    return (
        f"Started {result['mode']} ({result['coverage'].replace('_', ' ')}) for "
        f"{', '.join(result['rooms'])}{' in that order' if result.get('ordered') else ''}; "
        f"robot is now {result['activity']}."
    )
