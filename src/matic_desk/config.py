"""Paths, robot credential file, room aliases and logging.

Nothing in this module ever logs or prints the contents of robot.json.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .errors import MaticDeskError

APP_NAME = "Matic Desk"
ROBOT_KEYS = ("host", "port", "hostname", "serial", "fingerprint", "credential")

log = logging.getLogger(__name__)


def robot_config_path() -> Path:
    return Path(os.environ.get("MATIC_CONFIG") or "~/.config/matic/robot.json").expanduser()


def data_dir() -> Path:
    default = Path("~/Library/Application Support") / APP_NAME
    return Path(os.environ.get("MATIC_DESK_DATA") or default).expanduser()


def ledger_path() -> Path:
    return data_dir() / "ledger.sqlite"


def aliases_path() -> Path:
    return data_dir() / "aliases.json"


def log_path() -> Path:
    return Path("~/Library/Logs").expanduser() / APP_NAME / "backend.log"


# ------------------------------------------------------------------ robot.json


def load_robot_config(path: Path | None = None) -> dict[str, Any]:
    """Read robot.json or raise `not_paired`."""
    path = path or robot_config_path()
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        raise MaticDeskError(
            "not_paired", "No robot is paired yet. Run `matic-desk pair` in Terminal."
        ) from None
    except (OSError, ValueError):
        raise MaticDeskError(
            "not_paired", f"{path.name} could not be read. Run `matic-desk pair` in Terminal again."
        ) from None
    if not isinstance(config, dict) or any(not config.get(key) for key in ROBOT_KEYS):
        raise MaticDeskError(
            "not_paired", f"{path.name} is incomplete. Run `matic-desk pair` in Terminal again."
        )
    return config


def read_robot_config(path: Path | None = None) -> dict[str, Any]:
    """robot.json as stored, or `{}` when it is missing or unreadable. Never validated."""
    try:
        config = json.loads((path or robot_config_path()).read_text())
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def save_robot_config(config: dict[str, Any], path: Path | None = None) -> Path:
    """Write robot.json atomically with mode 0600."""
    path = path or robot_config_path()
    _atomic_write(path, json.dumps(config, indent=2) + "\n", mode=0o600)
    return path


def update_robot_config(updates: dict[str, Any], path: Path | None = None) -> Path:
    """Merge `updates` into robot.json, keeping keys other tools store there (e.g. `bag_swapped_at`)."""
    path = path or robot_config_path()
    return save_robot_config({**read_robot_config(path), **updates}, path)


# --------------------------------------------------------------------- aliases


def load_aliases(path: Path | None = None) -> dict[str, list[str]]:
    path = path or aliases_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("Ignoring unreadable aliases file %s", path)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): [str(room) for room in rooms]
        for name, rooms in raw.items()
        if isinstance(rooms, list)
    }


def save_aliases(aliases: dict[str, list[str]], path: Path | None = None) -> None:
    path = path or aliases_path()
    _atomic_write(path, json.dumps(aliases, indent=2, sort_keys=True) + "\n", mode=0o644)


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------- logging


def setup_logging(*, to_file: bool = True, level: int = logging.INFO) -> None:
    """Log to stderr (and the backend log file). Never to stdout."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(name)s[%(process)d] %(levelname)s %(message)s")
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)
    if to_file:
        try:
            log_path().parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(formatter)
            root.addHandler(handler)
        except OSError as err:
            log.warning("Could not open log file: %s", err)
    root.setLevel(level)
    for noisy in ("grpclib", "h2", "hpack", "asyncio", "PIL", "bleak", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Newer MCP clients probe with methods this SDK predates (e.g. "server/discover");
    # the SDK answers with an error and logs a 30-line validation dump. Keep it out.
    root.addFilter(lambda record: not record.getMessage().startswith("Failed to validate request"))
