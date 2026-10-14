# Vendored code

## `hermes_client/`

The local-network protocol client for Matic robots, copied from the
[matic-home-assistant](https://github.com/ProspectOre/matic-home-assistant)
Home Assistant integration.

| | |
| --- | --- |
| Upstream | <https://github.com/ProspectOre/matic-home-assistant> |
| Commit | `78ce104f6775d4bf0a3574c92facb12602301ccf` |
| Directory | `custom_components/matic_robot/client/` |
| Changes | None. The Python sources, protos and certificate are unmodified; the only addition is a provenance note at the top of `hermes_client/LICENSE`. |
| License | MIT, see [`hermes_client/LICENSE`](hermes_client/LICENSE) |

Matic Desk imports it as `matic_desk.vendor.hermes_client`. The client uses
relative imports only, so it works from this location without edits.

Do not edit files in `hermes_client/`. Put adaptations in Matic Desk's own
modules instead, so the directory can be replaced wholesale.

### Updating

1. Copy `custom_components/matic_robot/client/` from a newer upstream commit
   over `hermes_client/` (keep the provenance note in `LICENSE`).
2. Update the commit hash here and in `hermes_client/LICENSE`.
3. Run `uv run pytest`, then check `uv run matic-desk status` against a robot.

Firmware updates can change the protocol; upstream is usually the first place a
fix lands.
