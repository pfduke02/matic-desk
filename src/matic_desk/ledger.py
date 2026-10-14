"""Local SQLite ledger of cleaning runs, bag swaps and bag events.

The robot keeps only its ~20 most recent runs, so every history read is copied
here; bag usage is computed from this ledger (docs/protocol.md, "Bag report").
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GUIDANCE_DAYS = 7  # Matic's guidance: about a week of daily vacuum + mop per bag
BAG_FLAGS = ("bag_full", "bag_missing")
SETUP_SECONDS = 10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    key TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER,
    vacuum_seconds INTEGER,
    mop_seconds INTEGER,
    rooms TEXT NOT NULL DEFAULT '[]',
    modes TEXT NOT NULL DEFAULT '[]',
    completed INTEGER,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_started_at ON sessions (started_at);
CREATE TABLE IF NOT EXISTS bag_swaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    swapped_at TEXT NOT NULL,
    note TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind_at ON events (kind, at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_UPSERT = """
INSERT INTO sessions (key, started_at, ended_at, duration_seconds, vacuum_seconds,
                      mop_seconds, rooms, modes, completed, first_seen_at, updated_at)
VALUES (:key, :started_at, :ended_at, :duration_seconds, :vacuum_seconds,
        :mop_seconds, :rooms, :modes, :completed, :now, :now)
ON CONFLICT (key) DO UPDATE SET
    started_at = excluded.started_at,
    ended_at = excluded.ended_at,
    duration_seconds = excluded.duration_seconds,
    vacuum_seconds = excluded.vacuum_seconds,
    mop_seconds = excluded.mop_seconds,
    rooms = excluded.rooms,
    modes = excluded.modes,
    completed = excluded.completed,
    updated_at = excluded.updated_at
WHERE sessions.started_at IS NOT excluded.started_at
   OR sessions.ended_at IS NOT excluded.ended_at
   OR sessions.duration_seconds IS NOT excluded.duration_seconds
   OR sessions.vacuum_seconds IS NOT excluded.vacuum_seconds
   OR sessions.mop_seconds IS NOT excluded.mop_seconds
   OR sessions.rooms IS NOT excluded.rooms
   OR sessions.modes IS NOT excluded.modes
   OR sessions.completed IS NOT excluded.completed
"""


def iso(moment: datetime) -> str:
    """Contract timestamp: UTC, whole seconds, `Z` suffix."""
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def normalize_time(value: str | None) -> str | None:
    moment = parse_iso(value)
    return iso(moment) if moment else None


class Ledger:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._setup()

    def _setup(self) -> None:
        """Switch to WAL and create tables. Retried: when several processes (the
        app's bridge, an MCP server, the CLI) create the ledger at the same moment,
        SQLite reports "database is locked" without waiting for the busy timeout.
        """
        deadline = time.monotonic() + SETUP_SECONDS
        while True:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                with self._db:
                    self._db.executescript(SCHEMA)
                return
            except sqlite3.OperationalError as err:
                busy = "locked" in str(err) or "busy" in str(err)
                if not busy or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02 + random.random() * 0.08)

    def close(self) -> None:
        self._db.close()

    # ---- meta

    def meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---- sessions

    def ingest(self, sessions: list[dict[str, Any]], now: datetime) -> bool:
        """Upsert contract Session dicts. Returns True when anything changed."""
        stamp = iso(now)
        with self._db:
            if self.meta("first_ingest_at") is None:
                self._set_meta("first_ingest_at", stamp)
            before = self._db.total_changes
            for session in sessions:
                completed = session.get("completed")
                self._db.execute(
                    _UPSERT,
                    {
                        "key": session["key"],
                        "started_at": normalize_time(session.get("started_at")),
                        "ended_at": normalize_time(session.get("ended_at")),
                        "duration_seconds": session.get("duration_seconds"),
                        "vacuum_seconds": session.get("vacuum_seconds"),
                        "mop_seconds": session.get("mop_seconds"),
                        "rooms": json.dumps(list(session.get("rooms") or [])),
                        "modes": json.dumps(list(session.get("modes") or [])),
                        "completed": None if completed is None else int(bool(completed)),
                        "now": stamp,
                    },
                )
            return self._db.total_changes > before

    def sessions(self) -> list[dict[str, Any]]:
        """All stored runs, oldest first, as contract Session dicts."""
        rows = self._db.execute(
            "SELECT * FROM sessions ORDER BY started_at IS NULL, started_at, key"
        ).fetchall()
        return [
            {
                "key": row["key"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "duration_seconds": row["duration_seconds"],
                "vacuum_seconds": row["vacuum_seconds"],
                "mop_seconds": row["mop_seconds"],
                "rooms": json.loads(row["rooms"]),
                "modes": json.loads(row["modes"]),
                "completed": None if row["completed"] is None else bool(row["completed"]),
            }
            for row in rows
        ]

    # ---- bag swaps and events

    def record_swap(self, at: datetime, note: str | None, now: datetime) -> int:
        """Record a swap; it becomes the current bag. Returns how many swaps it replaced.

        A swap dated at or before already recorded ones is a correction (e.g. "I
        pressed Record by mistake, it was Oct 6"): those later swaps are removed.
        """
        with self._db:
            replaced = self._db.execute("DELETE FROM bag_swaps WHERE swapped_at >= ?", (iso(at),)).rowcount
            self._db.execute(
                "INSERT INTO bag_swaps (swapped_at, note, recorded_at) VALUES (?, ?, ?)",
                (iso(at), note, iso(now)),
            )
        return replaced

    def import_swap_once(self, at: datetime | None, now: datetime) -> bool:
        """Adopt an outside swap date as the first swap, only if none was ever recorded.

        Checked once per ledger, so a later correction is never undone by a re-import.
        """
        with self._db:
            if self.meta("swap_import_checked") is not None:
                return False
            self._set_meta("swap_import_checked", iso(now))
            if at is None:
                return False
            cursor = self._db.execute(
                "INSERT INTO bag_swaps (swapped_at, note, recorded_at) "
                "SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM bag_swaps)",
                (iso(at), "imported from robot.json (bag_swapped_at)", iso(now)),
            )
            return cursor.rowcount == 1

    def swaps(self) -> list[datetime]:
        rows = self._db.execute("SELECT swapped_at FROM bag_swaps ORDER BY swapped_at, id").fetchall()
        return [moment for row in rows if (moment := parse_iso(row["swapped_at"]))]

    def events(self, kind: str) -> list[datetime]:
        rows = self._db.execute("SELECT at FROM events WHERE kind = ? ORDER BY at, id", (kind,))
        return [moment for row in rows if (moment := parse_iso(row["at"]))]

    def record_bag_flags(
        self, bag_full: bool | None, bag_missing: bool | None, at: datetime
    ) -> list[str]:
        """Record `<flag>_on` / `<flag>_off` transitions. `None` (not reported) is ignored."""
        recorded: list[str] = []
        with self._db:
            for flag, value in zip(BAG_FLAGS, (bag_full, bag_missing), strict=True):
                if value is None:
                    continue
                previous = self.meta(f"last_{flag}")
                current = "1" if value else "0"
                if previous == current:
                    continue
                if value or previous is not None:  # a first "off" is not a transition
                    kind = f"{flag}_{'on' if value else 'off'}"
                    self._db.execute("INSERT INTO events (at, kind) VALUES (?, ?)", (iso(at), kind))
                    recorded.append(kind)
                self._set_meta(f"last_{flag}", current)
        return recorded

    # ---- bag report

    def bag_report(self, now: datetime, status: dict[str, Any] | None) -> dict[str, Any]:
        connected = bool(status and status.get("connected"))
        bag_full = status.get("bag_full") if connected and status else None
        bag_missing = status.get("bag_missing") if connected and status else None
        in_progress = connected and status is not None and status.get("activity") == "cleaning"

        swaps = self.swaps()
        if not swaps:
            return {
                "swapped_at": None,
                "days_since": None,
                "runs_since": None,
                "cleaning_seconds_since": None,
                "vacuum_seconds_since": None,
                "mop_seconds_since": None,
                "counted_from": None,
                "complete": False,
                "in_progress": in_progress,
                "bag_full": bag_full,
                "bag_missing": bag_missing,
                "last_bag_full_at": None,
                "previous_bags": [],
                "estimate": {
                    "basis": "unknown",
                    "days_left": None,
                    "vacuum_hours_left": None,
                    "text": _full_prefix(bag_full)
                    + "No estimate yet: record the date the bag was last changed to start counting.",
                },
            }

        runs = [
            (start, session)
            for session in self.sessions()
            if (start := parse_iso(session["started_at"])) is not None
        ]
        full_events = self.events("bag_full_on")
        first_ingest = parse_iso(self.meta("first_ingest_at"))
        swapped = swaps[-1]

        current_runs = [s for start, s in runs if start >= swapped]
        starts_since = [start for start, _ in runs if start >= swapped]
        complete = _covered_from(swapped, first_ingest, runs)
        if (first_ingest is not None and first_ingest <= swapped) or not starts_since:
            counted_from = swapped
        else:
            counted_from = min(starts_since)
        vacuum_since = _total(current_runs, "vacuum_seconds")
        days_since = round(max(0.0, (now - swapped).total_seconds()) / 86400, 1)
        fulls_since = [moment for moment in full_events if moment >= swapped]

        previous = [
            _previous_bag(start, end, runs, full_events, _covered_from(start, first_ingest, runs))
            for start, end in zip(swaps, swaps[1:], strict=False)
        ]
        previous.reverse()  # newest first

        return {
            "swapped_at": iso(swapped),
            "days_since": days_since,
            "runs_since": len(current_runs),
            "cleaning_seconds_since": _total(current_runs, "duration_seconds"),
            "vacuum_seconds_since": vacuum_since,
            "mop_seconds_since": _total(current_runs, "mop_seconds"),
            "counted_from": iso(counted_from),
            "complete": complete,
            "in_progress": in_progress,
            "bag_full": bag_full,
            "bag_missing": bag_missing,
            "last_bag_full_at": iso(fulls_since[-1]) if fulls_since else None,
            "previous_bags": previous,
            "estimate": _estimate(previous, days_since, vacuum_since, bag_full),
        }


def _total(sessions: list[dict[str, Any]], field: str) -> int:
    return int(sum(session.get(field) or 0 for session in sessions))


def _covered_from(
    swapped: datetime, first_ingest: datetime | None, runs: list[tuple[datetime, dict[str, Any]]]
) -> bool:
    """The ledger holds every run since `swapped` (the contract's `complete` rule)."""
    return (first_ingest is not None and first_ingest <= swapped) or any(start < swapped for start, _ in runs)


def _previous_bag(
    start: datetime,
    end: datetime,
    runs: list[tuple[datetime, dict[str, Any]]],
    full_events: list[datetime],
    complete: bool,
) -> dict[str, Any]:
    bag_runs = [(moment, s) for moment, s in runs if start <= moment < end]
    first_full = next((moment for moment in full_events if start <= moment < end), None)
    # Only a bag the ledger saw from its swap gives a usable capacity: runs the
    # robot had already forgotten would make it look smaller than it was.
    to_full = (
        _total([s for moment, s in bag_runs if moment < first_full], "vacuum_seconds")
        if first_full is not None and complete
        else None
    )
    return {
        "swapped_at": iso(start),
        "replaced_at": iso(end),
        "cleaning_seconds": _total([s for _, s in bag_runs], "duration_seconds"),
        "vacuum_seconds": _total([s for _, s in bag_runs], "vacuum_seconds"),
        "vacuum_seconds_to_full": to_full,
    }


def _full_prefix(bag_full: bool | None) -> str:
    return "The robot reports the bag is full and is asking for a swap now. " if bag_full else ""


def _estimate(
    previous: list[dict[str, Any]], days_since: float, vacuum_since: int, bag_full: bool | None
) -> dict[str, Any]:
    capacities = [
        bag["vacuum_seconds_to_full"] for bag in previous if (bag["vacuum_seconds_to_full"] or 0) > 0
    ]
    if capacities:
        capacity = statistics.median(capacities)
        hours_left = round(max(0.0, capacity - vacuum_since) / 3600, 1)
        count = len(capacities)
        text = (
            f"Estimate from {count} previous bag{'s' if count != 1 else ''}: bags filled after "
            f"about {capacity / 3600:.1f} vacuum hours; this one has {vacuum_since / 3600:.1f}, "
            + (f"so about {hours_left:.1f} vacuum hours are left." if hours_left > 0 else "so it may be full soon.")
        )
        return {
            "basis": "learned",
            "days_left": None,
            "vacuum_hours_left": hours_left,
            "text": _full_prefix(bag_full) + text,
        }
    days_left = round(max(0.0, GUIDANCE_DAYS - days_since), 1)
    text = (
        f"Rough estimate: Matic suggests about {GUIDANCE_DAYS} days per bag with daily vacuum + mop; "
        f"this bag is {days_since:.1f} days old, "
        + (f"so about {days_left:.1f} days are left." if days_left > 0 else "so it is due for a swap.")
    )
    return {
        "basis": "guidance",
        "days_left": days_left,
        "vacuum_hours_left": None,
        "text": _full_prefix(bag_full) + text,
    }
