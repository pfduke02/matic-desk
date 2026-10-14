# Matic Desk

Run your Matic robot vacuum from the Mac menu bar, the terminal, or an AI
assistant (Claude Desktop, Claude Code, Codex). Everything stays on your local
network: no cloud, no account.

> [!WARNING]
> **Unofficial.** Matic Desk is a community project. It is not affiliated with,
> endorsed by, or supported by Matic Robots, Inc. It talks to the robot over a
> reverse-engineered local protocol, using the client from
> [ProspectOre/matic-home-assistant](https://github.com/ProspectOre/matic-home-assistant)
> (MIT). A robot firmware update can break it at any time. Use at your own risk.

## What's in the box

- **Menu bar app**: live robot status and bag report, plus quick controls.
- **`matic-desk`**: a command-line tool for the same things.
- **`matic-desk-mcp`**: an [MCP](https://modelcontextprotocol.io) server, so
  Claude and Codex can check on the robot and clean rooms by name.

All three share one Python service layer, so they see the same robot, the same
room names and the same bag ledger.

## Features

- **Status**: what the robot is doing (cleaning, paused, returning, charging,
  docked, ready, error), battery, current room, error codes, bag flags, firmware.
- **Clean by room**: by map name (`Kitchen 1`), by group (`kitchen` means every
  `Kitchen N` segment), by your own alias (`office`), or the whole floor.
  Vacuum, mop or both; quick, standard or heavy-duty coverage; optionally in
  the order you give.
- **Control**: pause, resume, stop, send to the dock.
- **Settings**: water flow (0.5 to 2.0), deep mop, pet waste, child lock, voice.
- **History and map**: recent runs, and a floor map PNG with the robot's
  position and current route.
- **Bag tracking**: see below.

### Bag tracking

The robot has **no bag fill sensor**. It only reports two flags: "bag full"
(raised when airflow is restricted) and "bag missing". So Matic Desk does the
bookkeeping itself:

- You record when you swap the bag: in the menu bar app, with
  `matic-desk bag swap`, or by telling Claude "I swapped the bag on Oct 6".
  Recorded the wrong date? Record the right one: an earlier date replaces the
  later swap.
- It adds up what has run since then: total cleaning time, **vacuum hours** and
  mop hours, number of runs, and days.
- It keeps its own ledger of runs, because the robot only remembers its last
  ~20. Runs are picked up whenever anything reads the history or bag report,
  and every 5 minutes while the menu bar app is open. If the ledger started
  after your swap, the report says that earlier runs may be missing.
- It estimates what's left. At first that is Matic's guidance of about a week
  per bag. Once a bag has filled up (the robot raised "bag full"), it learns how
  many vacuum hours your bags last and uses that instead.

These are **estimates**, not measurements.

### What it can't tell you

Water tank level and brush or roller wear. The robot doesn't publish them, so
neither can Matic Desk.

## Requirements

- macOS 14 or later. Tested on Apple Silicon; the app is built for the Mac
  you build it on.
- Apple Command Line Tools, for the Swift compiler: `xcode-select --install`.
  Full Xcode is not needed. Only required for the menu bar app.
- [uv](https://docs.astral.sh/uv/). It fetches Python 3.14 for you.
- A Matic robot on the same network, and the Matic phone app (used once, to
  turn on Pairing mode).

## Install

```sh
git clone https://github.com/pfduke02/matic-desk.git
cd matic-desk
scripts/install.sh
```

The script:

1. checks macOS, uv and the Swift compiler;
2. runs `uv sync`, which creates `.venv/` inside the repo;
3. builds `Matic Desk.app` and copies it to `~/Applications` (quitting a
   running copy first);
4. prints ready-to-paste MCP config for Claude Desktop and Codex with your real
   path, and a pairing reminder if you haven't paired yet.

| Option | Effect |
| --- | --- |
| `--dry-run` | Print what it would do; change nothing. |
| `--no-app` | Skip the menu bar app (CLI and MCP server only). |
| `--register-claude-code` | Also run `claude mcp add` for you (skipped if `matic-desk` is already registered). |

It is safe to re-run, for example after `git pull`. It never edits Claude
Desktop or Codex config files. Keep the clone where it is: the app and the MCP
config point into its `.venv`.

## Pair with your robot (one time)

Pairing uses Bluetooth once to get a credential. After that everything goes
over Wi-Fi.

1. In the Matic app on your phone, go to **Settings → Connectivity → Add
   another user** and turn on **Pairing mode**.
2. On the Mac, open **Terminal or iTerm yourself** and run:

   ```sh
   cd /ABSOLUTE/PATH/matic-desk
   uv run matic-desk pair
   ```

   This must run in a terminal you opened, not from Claude, Codex or another
   app: macOS kills Bluetooth access for processes started by other apps.

   macOS also only lets a process use Bluetooth from inside an app bundle that
   says why it needs it, which a plain Python (or Terminal) doesn't. So `pair`
   restarts itself inside a tiny helper, `Matic Desk Pairing.app` (an
   Info.plist plus a link to the project's Python), which it creates in
   `~/Library/Application Support/Matic Desk/`. If macOS asks whether
   "Matic Desk Pairing" may use Bluetooth, allow it.
3. When the robot shows a **six-digit code**, type it into the Mac's
   **Bluetooth pairing dialog** (not the terminal).

The credential is saved to `~/.config/matic/robot.json` with mode 0600 (other
keys already in that file are kept). Check that it worked with
`uv run matic-desk status`. If the robot is ever factory reset, pair again; the
menu bar app and MCP server pick up the new pairing without a restart.

## Menu bar app

Open **Matic Desk** from `~/Applications`. It shows what the robot is doing,
battery, current room, errors and the bag report, and lets you start a clean,
pause, resume, stop or dock, and record a bag swap. Status updates live.

The app runs the Python backend (`matic-desk-bridge`) from the repo's `.venv`.
Leaving it open keeps the bag ledger up to date.

**Local Network permission.** On first launch macOS asks whether Matic Desk may
find devices on your local network. Allow it; without it the robot shows as
offline. To change it later: **System Settings → Privacy & Security → Local
Network**, then turn on Matic Desk.

## Use it from Claude and Codex

Every client runs the same MCP server:
`~/.local/share/matic-desk/venv/bin/matic-desk-mcp`. `scripts/install.sh`
prints the exact path for your machine.

### Claude Code

```sh
claude mcp add --scope user matic-desk -- ~/.local/share/matic-desk/venv/bin/matic-desk-mcp
```

Or run `scripts/install.sh --register-claude-code`.

### Claude Desktop

**Settings → Developer → Edit Config** opens `claude_desktop_config.json`
(in `~/Library/Application Support/Claude/`). Add:

```json
{
  "mcpServers": {
    "matic-desk": {
      "command": "~/.local/share/matic-desk/venv/bin/matic-desk-mcp"
    }
  }
}
```

If the file already has `mcpServers`, add the `matic-desk` entry inside it.
Then quit and reopen Claude Desktop.

### Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.matic-desk]
command = "~/.local/share/matic-desk/venv/bin/matic-desk-mcp"
```

> [!NOTE]
> macOS applies the Local Network permission to the app that starts the MCP
> server. Command-line tools you run in a terminal are generally exempt, but
> when Claude Desktop (or another app) starts it, macOS may ask whether *that
> app* can find devices on your local network. Allow it, or turn it on under
> **Privacy & Security → Local Network**; otherwise the robot shows as offline.

### Tools

| Tool | What it does |
| --- | --- |
| `matic_status` | Status and settings |
| `matic_rooms` | Room names, groups and aliases |
| `matic_bag_report` | Bag report and estimate |
| `matic_history` | Recent runs |
| `matic_map` | Floor map image with the robot and its route |
| `matic_start_cleaning` | Clean rooms or the whole floor |
| `matic_pause`, `matic_resume`, `matic_stop`, `matic_dock` | Control the current run |
| `matic_set_setting` | Change a setting |
| `matic_record_bag_swap` | Record a bag swap |
| `matic_name_room` | Save a room alias |

The first five only read. The rest act on the robot or your ledger.

### Things to say

- "Vacuum the kitchen."
- "Mop the living room on heavy duty."
- "How's the bag?"
- "I swapped the bag on Oct 6."
- "Send it home."
- "Call Room 3 the office, then vacuum the office."

"Vacuum" means vacuum only, "mop" means mop only, and plain "clean" does both.

## Command line

Run it from the repo with `uv run matic-desk ...`, or call
`~/.local/share/matic-desk/venv/bin/matic-desk` directly. Put `--json` before
the command (`matic-desk --json status`) to get the raw objects from
[docs/protocol.md](docs/protocol.md).

| Command | What it does |
| --- | --- |
| `status` | Activity, battery, room, errors, bag flags |
| `settings` | Water flow, deep mop, pet waste, child lock, voice |
| `rooms` | Room names, groups and aliases |
| `history [-n N]` | Recent runs, newest first (also saved to the ledger) |
| `bag` | Time and vacuum/mop hours since the last swap, plus the estimate |
| `bag swap [--at DATE] [--note TEXT]` | Record a swap; default now, `DATE` like `2026-10-06` (local time) |
| `clean ROOM... [--all] [--mode vacuum\|mop\|both] [--coverage quick\|standard\|heavy_duty] [--ordered]` | Start cleaning (defaults: `both`, `standard`) |
| `pause`, `resume`, `stop`, `dock` | Control the current run |
| `set SETTING VALUE` | `water_flow` 0.5 to 2.0; `deep_mop`, `pet_waste`, `child_lock`, `voice` on or off |
| `map [--out FILE] [--no-route]` | Save the floor map as a PNG (default: `matic-map.png` in the temp folder; the path is printed) |
| `alias NAME ROOM...` / `unalias NAME` | Name a set of rooms, e.g. `alias office "Room 3"` |
| `pair` | One-time Bluetooth pairing (run it in Terminal yourself) |

Room names ignore case and extra spaces, and are matched in this order: your
alias, the exact map name, a group (`kitchen` or `kitchens` matches
`Kitchen 1` and `Kitchen 2`), then `all`, `everything`, `whole floor` or
`whole house`. Anything else is an error that lists the valid names. An alias
can't reuse a room name, a group name or one of those whole-floor words.

## Files on disk

| Path | What |
| --- | --- |
| `~/.config/matic/robot.json` | Robot identity and credential from `pair`, mode 0600. Override with `MATIC_CONFIG`. |
| `~/Library/Application Support/Matic Desk/ledger.sqlite` | Cleaning runs, bag swaps and bag events |
| `~/Library/Application Support/Matic Desk/aliases.json` | Your room aliases |
| `~/Library/Application Support/Matic Desk/Matic Desk Pairing.app` | Bluetooth helper used only by `pair` |
| `~/Library/Logs/Matic Desk/backend.log` | Backend log (no credential, no map data) |
| `~/Applications/Matic Desk.app` | The menu bar app |

Set `MATIC_DESK_DATA` to keep the ledger and aliases somewhere else.

To uninstall: quit and delete the app, run `claude mcp remove matic-desk` (and
remove the Claude Desktop or Codex entry if you added one), delete the clone,
and delete the files above.

## Privacy

- Matic Desk talks only to your robot, on your local network (gRPC over TLS,
  pinned to the robot's certificate). No cloud, no account, no analytics or
  telemetry.
- The credential stays in `robot.json` and is never logged or printed. The
  ledger, aliases and logs stay on your Mac.
- When you use it through an AI assistant, tool results (status, room names,
  history, the map image) become part of that conversation and are sent to the
  assistant's provider, like anything else you share there.

## How it works

```text
Menu bar app (Swift) ── JSON lines ──▶ matic-desk-bridge ─┐
Claude / Codex ──────── MCP (stdio) ─▶ matic-desk-mcp ────┼─▶ MaticService ─▶ hermes_client ─▶ robot
Terminal ────────────────────────────▶ matic-desk ────────┘        │            (gRPC/TLS on your LAN)
                                                                   └─▶ ledger.sqlite, aliases.json
```

- `MaticService` (`src/matic_desk/service.py`) does the work and returns plain
  objects. The bridge, MCP server and CLI only reshape or print them.
- [docs/protocol.md](docs/protocol.md) is the contract: object shapes, errors,
  the bridge protocol and the MCP tools.
- The protocol client is vendored, unmodified, in
  [src/matic_desk/vendor](src/matic_desk/vendor/README.md).

## Development

```sh
uv sync
uv run pytest
```

Tests use fakes; they never touch the network or your real ledger. When trying
things by hand, point `MATIC_DESK_DATA` at a scratch directory:

```sh
MATIC_DESK_DATA="$(mktemp -d)" uv run matic-desk bag
```

The menu bar app is plain Swift built with `swiftc`, no Xcode project:
`app/build.sh [--backend-python PATH] [--out DIR]` produces
`build/Matic Desk.app`. `app/dev/fake_bridge.py` speaks the bridge protocol with
canned data, so you can work on the UI without a robot.

If a firmware update breaks things, check
[matic-home-assistant](https://github.com/ProspectOre/matic-home-assistant)
for a newer client first.

## Credits

- The local protocol client comes from
  [ProspectOre/matic-home-assistant](https://github.com/ProspectOre/matic-home-assistant)
  (MIT). Figuring out the protocol was their work.
- "Matic" is a trademark of Matic Robots, Inc., used here only to say which
  robot this works with.

## License

MIT, see [LICENSE](LICENSE). The vendored client in
`src/matic_desk/vendor/hermes_client` keeps its own MIT license.
