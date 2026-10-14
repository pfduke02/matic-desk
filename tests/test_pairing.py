"""Pairing helpers and robot.json handling. Nothing here touches Bluetooth or the network."""

import json
import os
import plistlib
import site
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from matic_desk import pairing
from matic_desk.config import load_robot_config, read_robot_config, update_robot_config
from matic_desk.errors import MaticDeskError

PAIRED = {
    "host": "192.0.2.10",
    "port": 16320,
    "hostname": "matic-abcd-efgh",
    "serial": "TEST-SERIAL",
    "fingerprint": "00" * 32,
    "credential": "placeholder",
}


def test_fingerprint_is_required(tmp_path):
    path = tmp_path / "robot.json"
    path.write_text(json.dumps({key: value for key, value in PAIRED.items() if key != "fingerprint"}))
    with pytest.raises(MaticDeskError) as caught:
        load_robot_config(path)
    assert caught.value.type == "not_paired"
    path.write_text(json.dumps(PAIRED))
    assert load_robot_config(path)["fingerprint"] == PAIRED["fingerprint"]


def test_update_keeps_other_keys_and_mode(tmp_path):
    path = tmp_path / "robot.json"
    path.write_text(json.dumps({**PAIRED, "bag_swapped_at": "2026-10-06T12:00:00+00:00"}))
    update_robot_config({"credential": "new", "paired_at": "2026-10-14T14:00:00+00:00"}, path)
    saved = read_robot_config(path)
    assert saved["bag_swapped_at"] == "2026-10-06T12:00:00+00:00"
    assert saved["credential"] == "new" and saved["host"] == PAIRED["host"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_robot_config_tolerates_missing_and_garbage(tmp_path):
    assert read_robot_config(tmp_path / "missing.json") == {}
    (tmp_path / "bad.json").write_text("[1, 2")
    assert read_robot_config(tmp_path / "bad.json") == {}


def test_bluetooth_shim_bundle(tmp_path):
    python = tmp_path / "bin" / "python3.14"
    python.parent.mkdir()
    python.touch()
    executable = pairing.ensure_bluetooth_shim(str(python))
    app = pairing.bluetooth_shim_path()
    assert app.parent == Path(os.environ["MATIC_DESK_DATA"])  # the data folder, never the repo
    assert executable == app / "Contents" / "MacOS" / "python3.14"
    assert os.readlink(executable) == str(python)
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["NSBluetoothAlwaysUsageDescription"]
    assert info["CFBundleExecutable"] == "python3.14" and info["CFBundlePackageType"] == "APPL"

    other = tmp_path / "other" / "python3.14"
    other.parent.mkdir()
    other.touch()
    pairing.ensure_bluetooth_shim(str(other))  # refreshed when the interpreter moves
    assert os.readlink(executable) == str(other)


def test_reexec_runs_the_cli_inside_the_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(pairing.sys, "platform", "darwin")
    monkeypatch.delenv(pairing.SHIM_ENV, raising=False)
    monkeypatch.setattr(pairing.os, "execve", lambda path, argv, env: calls.append((path, argv, env)))
    pairing.reexec_through_bluetooth_shim(["pair"])
    [(path, argv, env)] = calls
    assert Path(path).parent.parent.parent == pairing.bluetooth_shim_path()
    assert argv[1:3] == ["-c", pairing._BOOTSTRAP] and argv[-1] == "pair"
    assert env[pairing.SHIM_ENV] == "1"


@pytest.mark.parametrize("platform, inside", [("linux", False), ("darwin", True)])
def test_reexec_is_a_no_op_when_not_needed(monkeypatch, platform, inside):
    monkeypatch.setattr(pairing.sys, "platform", platform)
    if inside:
        monkeypatch.setenv(pairing.SHIM_ENV, "1")
    monkeypatch.setattr(pairing.os, "execve", lambda *args: pytest.fail("should not re-exec"))
    pairing.reexec_through_bluetooth_shim(["pair"])


def test_bootstrap_loads_this_environment(tmp_path):
    """The helper runs the bare interpreter, so the bootstrap must find matic_desk itself."""
    result = subprocess.run(
        [sys.executable, "-c", pairing._BOOTSTRAP, os.pathsep.join(site.getsitepackages()), "--version"],
        capture_output=True, text=True, cwd=tmp_path, timeout=60,
    )
    assert result.returncode == 0 and "matic-desk" in result.stdout
