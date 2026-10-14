# Matic Desk contract

Single source of truth for every front-end. `src/matic_desk/service.py` produces
these shapes; `bridge.py` (Swift app), `mcp_server.py` (Claude/Codex) and
`cli.py` only reshape or print them. Timestamps are ISO 8601 UTC with a `Z`
suffix (`2026-01-13T14:20:00Z`). Durations are integer seconds. `null` means
"the robot did not say", never "false" or "zero". All example values below are
invented.

## Files on disk

| Path | Purpose |
| --- | --- |
| `~/.config/matic/robot.json` (override: `MATIC_CONFIG`) | Robot identity + credential written by `matic-desk pair`. Mode 0600. Same format the original skill uses: `host, port, hostname, serial, name, hardware_revision, fingerprint, app_id, credential, paired_at` (`host, port, hostname, serial, fingerprint, credential` are required, else `not_paired`). Other tools may keep extra keys there (the original skill stores `bag_swapped_at`); `pair` merges its keys in and keeps the rest. Never logged, never printed, never committed. |
| `~/Library/Application Support/Matic Desk/ledger.sqlite` (override: `MATIC_DESK_DATA`) | Local log of cleaning runs, bag swaps and bag events. |
| `~/Library/Application Support/Matic Desk/aliases.json` | Room aliases, e.g. `{"office": ["Room 3"]}`. |
| `~/Library/Application Support/Matic Desk/Matic Desk Pairing.app` | Helper bundle created by `matic-desk pair`: an Info.plist declaring `NSBluetoothAlwaysUsageDescription` plus a symlink to the Python running Matic Desk. macOS kills processes that use Bluetooth outside such a bundle, so `pair` re-executes itself inside it (env `MATIC_DESK_BT_SHIM=1`). |
| `~/Library/Logs/Matic Desk/backend.log` | Bridge / MCP logs (stderr is also used). No credential, no map data. |

Connection: connect to `host:port` but pass `hostname` (with `.local` appended
when it has no dot) so a DHCP address change is resolved over Bonjour; pin
`serial` and `fingerprint`.

## Objects

### Status
```json
{
  "connected": true,
  "robot_name": "matic-abcd-efgh",
  "activity": "cleaning",
  "battery": 99,
  "charging": false,
  "low_charge": false,
  "current_room": "Living Room",
  "errors": [{"code": 206, "label": "bag full"}],
  "bag_full": true,
  "bag_missing": false,
  "firmware": "v100.0",
  "updated_at": "2026-01-13T14:20:00Z",
  "message": null
}
```
- `activity` ∈ `cleaning | paused | returning | charging | docked | ready | error | offline`.
  `offline` (with `connected: false` and `message` explaining why, e.g. not paired,
  robot unreachable, macOS blocked Local Network access) is returned instead of
  raising, so a UI can always render.
- `current_room`: robot label with a leading `"the "` removed (`"the Kitchen"` → `"Kitchen"`).
- Error labels: 205 `bag missing`, 206 `bag full`, otherwise `error <code>`.
- `bag_full` / `bag_missing`: `true/false` when the robot reported its error list,
  `null` when it did not (the wire format cannot distinguish "no errors" from "not
  reported"). UIs show `null` as "no bag error reported".

### Settings
```json
{"water_flow": 1.0, "deep_mop": true, "pet_waste": false, "child_lock": false, "voice": true}
```
Any value may be `null`. `water_flow` is 0.5–2.0 in 0.1 steps.

### Rooms
```json
{
  "floor": null,
  "rooms": [{"name": "Kitchen 1"}, {"name": "Kitchen 2"}, {"name": "Living Room 1"}],
  "groups": {"Kitchen": ["Kitchen 1", "Kitchen 2"], "Living Room": ["Living Room 1"]},
  "aliases": {"office": ["Room 3"]}
}
```
`groups` collapses map segments named `<Base> <digits>` under `<Base>`.

### Room resolution (used by `clean` and `name_room`)
Input is a list of strings. Each string resolves, case- and whitespace-insensitively, by the first rule that matches:
1. alias name → its rooms
2. exact room name
3. group base (`"kitchen"` → `Kitchen 1, Kitchen 2`); a trailing plural `s` is tolerated
4. `"all" | "everything" | "whole floor" | "whole house"` → every room
Otherwise fail with `room_not_found`, listing room names and aliases. Order is
preserved; duplicates are dropped.

`name_room` rejects (`invalid_request`) an alias that would shadow a room name,
a group base (or its plural) or a whole-floor word, since aliases resolve first.

### Session (history item)
```json
{
  "key": "a1b2…",
  "started_at": "2026-01-12T15:00:00Z",
  "ended_at": "2026-01-12T15:15:00Z",
  "duration_seconds": 900,
  "vacuum_seconds": 300,
  "mop_seconds": 400,
  "rooms": ["Kitchen 2", "Kitchen 1"],
  "modes": ["mop", "vacuum"],
  "completed": true
}
```
`vacuum_seconds` = sum of `mode_results[].duration_seconds` where `cleaning_mode == "vacuum"`; `mop_seconds` likewise for `"mop"`. `duration_seconds` is the whole run (includes driving, docking). `key` is the hex of the robot's opaque record key and is the ledger primary key.

### Bag report
```json
{
  "swapped_at": "2026-01-05T12:00:00Z",
  "days_since": 8.1,
  "runs_since": 15,
  "cleaning_seconds_since": 50880,
  "vacuum_seconds_since": 34800,
  "mop_seconds_since": 14300,
  "counted_from": "2026-01-05T15:00:00Z",
  "complete": false,
  "in_progress": true,
  "bag_full": false,
  "bag_missing": false,
  "last_bag_full_at": null,
  "previous_bags": [
    {"swapped_at": "2025-12-29T12:00:00Z", "replaced_at": "2026-01-05T12:00:00Z",
     "cleaning_seconds": 48000, "vacuum_seconds": 31000, "vacuum_seconds_to_full": null}
  ],
  "estimate": {"basis": "guidance", "days_left": 0.0, "vacuum_hours_left": null,
               "text": "Rough estimate: Matic suggests about 7 days per bag with daily vacuum + mop; this bag is 8.1 days old, so it is due for a swap."}
}
```
Rules:
- `swapped_at` = latest recorded swap, or `null` (then all `*_since` fields are `null`, `estimate.basis = "unknown"`, text asks the user to record a swap).
- Recording a swap dated at or before already recorded swaps is a correction: those swaps are removed, so the swap just recorded is always the current one (there is no separate undo).
- Once per ledger, if no swap was ever recorded and robot.json has the original skill's `bag_swapped_at`, that date is imported as the first swap.
- A run counts toward a bag when `started_at >= swapped_at` (and `< next swap` for `previous_bags`).
- `counted_from` = the earliest run start the ledger has at or after the swap (or the swap time itself if the ledger's first ingest predates the swap). `complete` = the ledger's first ingest (meta `first_ingest_at`) is `<= swapped_at`, or the ledger holds a run that started before `swapped_at`. When `complete` is false, UIs must say runs before `counted_from` may be missing.
- `in_progress` = the robot is cleaning right now (that run's time is added once it appears in history).
- `last_bag_full_at` = latest `bag_full_on` event since the swap.
- `vacuum_seconds_to_full` (previous bags) = vacuum seconds from that bag's swap to its first `bag_full_on` event, else `null`. Also `null` when the ledger did not cover that bag from its swap (same rule as `complete`, applied to that bag's swap), so forgotten runs never shrink the learned capacity.
- Estimate: if at least one previous bag has `vacuum_seconds_to_full`, `basis = "learned"`, capacity = median of those, `vacuum_hours_left = max(0, capacity − vacuum_seconds_since)/3600`. Otherwise `basis = "guidance"`, `days_left = max(0, 7 − days_since)`. `text` always says it is an estimate. If `bag_full` is true, `text` says the robot is asking for a swap now.

### Clean result
```json
{"accepted": true, "rooms": ["Living Room 1", "Living Room 2"], "mode": "vacuum",
 "coverage": "standard", "ordered": false, "activity": "cleaning"}
```
`mode` ∈ `vacuum | mop | both`, `coverage` ∈ `quick | standard | heavy_duty`.
`activity` is re-read up to ~8 s after sending until it leaves docked/charging/ready.

### Command result
```json
{"sent": "dock", "activity": "returning"}
```

## Errors
Raised as `MaticDeskError(type, message)`; front-ends surface `type` and `message`.

| type | meaning |
| --- | --- |
| `not_paired` | no `robot.json`; run `matic-desk pair` in Terminal |
| `offline` | robot unreachable / TLS failed / Local Network permission denied |
| `room_not_found` | room resolution failed (message lists valid names) |
| `invalid_request` | bad parameter (mode, coverage, setting, value, date) |
| `robot_error` | robot rejected or failed the request |

## Service functions (`matic_desk.service.MaticService`, all async)
```
status() -> Status
settings() -> Settings
rooms() -> Rooms
history(limit: int = 10) -> {"sessions": [Session]}          # newest first; ingests the robot's list, then returns
                                                             # the ledger's runs (so a cut-short read hides nothing)
bag_report() -> BagReport                                    # refreshes history + bag flags first (best effort)
record_bag_swap(at: str | None = None, note: str | None = None) -> BagReport   # at = ISO date/datetime, local time if no offset; default now.
                                                             # A bare date equal to today's local date means now (not midnight).
clean(rooms: list[str] | None = None, whole_floor: bool = False, mode: str = "both",
      coverage: str = "standard", ordered: bool = False) -> CleanResult
      # rooms empty and whole_floor false -> invalid_request
command(name: "pause" | "resume" | "stop" | "dock") -> CommandResult
set_setting(setting: "water_flow" | "deep_mop" | "pet_waste" | "child_lock" | "voice", value) -> Settings
map_png(route: bool = True) -> bytes                          # 1024x1024 PNG, rooms + robot + route if active
name_room(alias: str, rooms: list[str]) -> Rooms
remove_room_alias(alias: str) -> Rooms
close() -> None
```
`MaticService(robot_factory=..., ledger=...)` accepts injected fakes for tests.
One robot connection is kept open per process and reopened on failure. Concurrent
callers share one connection attempt; after a failed attempt, calls fail fast with
`offline` for 20 s instead of queueing more ~10 s attempts. The robot is rebuilt
when robot.json changes, so re-pairing takes effect without a restart. History
reads are serialized and wait up to 2 s for each record (the robot's stream has
no end marker, and overlapping reads were cut short).

## Bridge protocol (Swift app ↔ `matic-desk-bridge`, JSON lines on stdio)
- stdout carries only protocol lines; logs go to stderr / the log file.
- Request: `{"id": 7, "method": "status", "params": {}}`
- Success: `{"id": 7, "ok": true, "result": {…}}`
- Failure: `{"id": 7, "ok": false, "error": {"type": "offline", "message": "…"}}`
- Events (no `id`): `{"event": "status", "data": Status}` whenever activity, battery, room, errors or bag flags change (live robot stream, with a 30 s poll fallback); `{"event": "bag", "data": BagReport}` after a background history ingest (every 5 min and when a run ends) changes it.
- Methods: `ping` → `{"pong": true}`, `status`, `settings`, `rooms`, `history {limit}`, `bag`, `record_bag_swap {at?, note?}`, `clean {rooms, whole_floor, mode, coverage, ordered}`, `command {name}`, `set_setting {setting, value}`, `map {route}` → `{"png_base64": "…"}`, `name_room {alias, rooms}`, `remove_room_alias {alias}`.
- Requests are handled concurrently; responses may arrive out of order.
- The bridge exits cleanly on stdin EOF.

## MCP tools (`matic-desk-mcp`, stdio)
| tool | maps to | notes |
| --- | --- | --- |
| `matic_status` | status (+ settings) | read-only |
| `matic_rooms` | rooms | read-only |
| `matic_bag_report` | bag_report | read-only |
| `matic_history` | history(limit) | read-only |
| `matic_map` | map_png | returns a PNG image |
| `matic_start_cleaning` | clean | `mode`: "vacuum" when the user says vacuum, "mop" for mop, "both" when they just say clean |
| `matic_pause` / `matic_resume` / `matic_stop` / `matic_dock` | command | |
| `matic_set_setting` | set_setting | |
| `matic_record_bag_swap` | record_bag_swap | omit `when` for today/now; an earlier day as a local date such as "2026-01-05" (the model converts "yesterday"); an earlier date than the current swap corrects it |
| `matic_name_room` | name_room | |
Tool results return the contract object as structured content plus a one-line human summary.
