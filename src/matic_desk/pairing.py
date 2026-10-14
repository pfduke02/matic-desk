"""One-time pairing: find the robot over Bonjour, get a credential, save robot.json.

Credentials come over the LAN when the firmware allows it, otherwise over
Bluetooth (bleak). The credential is written to robot.json (mode 0600) and is
never printed or logged.

macOS kills a process that touches CoreBluetooth unless it runs inside an app
bundle whose Info.plist declares NSBluetoothAlwaysUsageDescription; a plain
Python, and Terminal itself, have none. So `matic-desk pair` restarts itself
inside a tiny helper bundle (an Info.plist plus a symlink to this Python),
created on demand in Matic Desk's data folder.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import re
import site
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import data_dir, read_robot_config, update_robot_config
from .errors import MaticDeskError
from .robot import bonjour_hostname, local_timezone, map_error
from .vendor.hermes_client.api import MaticHermesClient
from .vendor.hermes_client.auth import HermesCredential, new_hermes_user_id
from .vendor.hermes_client.discovery import decode_bot_information
from .vendor.hermes_client.exceptions import MaticError, PairingModeRequiredError
from .vendor.hermes_client.models import RobotInfo
from .vendor.hermes_client.proto.hermes_auth_pb2 import BotToken, TokenRequest
from .vendor.hermes_client.tls import async_fetch_peer_certificate, validate_certificate

MDNS_SERVICE = "_matic_hermes._tcp"
MATIC_BLE_SERVICE = "5b14adcd-e995-9e80-c55a-b6c6fb6c612f"
TOKEN_CHARACTERISTIC = "84b52f26-d3b7-5ebe-ba52-ff38a447788d"

SHIM_ENV = "MATIC_DESK_BT_SHIM"
SHIM_NAME = "Matic Desk Pairing"
SHIM_BUNDLE_ID = "io.github.pfduke02.maticdesk.pairing"

TERMINAL_NOTE = f"""\
Matic Desk pairing
Run this from Terminal or iTerm yourself, not from Claude, Codex, an IDE or the
menu bar app: macOS stops processes those apps start from using Bluetooth.
Pairing can fall back to Bluetooth, which macOS only allows inside an app
bundle, so this command runs inside a small helper, "{SHIM_NAME}", kept in
Matic Desk's data folder. If macOS asks whether it may use Bluetooth, allow it.

Before you continue, in the Matic app: Settings > Connectivity > Add another
user > turn on Pairing mode.
"""

# Run by the helper's Python: load this environment's packages, then the CLI.
_BOOTSTRAP = (
    "import os, site, sys\n"
    "for path in sys.argv[1].split(os.pathsep): site.addsitedir(path)\n"
    "from matic_desk.cli import main\n"
    "sys.exit(main(sys.argv[2:]))\n"
)


def bluetooth_shim_path() -> Path:
    return data_dir() / f"{SHIM_NAME}.app"


def ensure_bluetooth_shim(python: str | None = None) -> Path:
    """Create or refresh the helper bundle; returns the executable to run."""
    target = Path(python or getattr(sys, "_base_executable", None) or sys.executable).absolute()
    app = bluetooth_shim_path()
    executable = app / "Contents" / "MacOS" / target.name
    info = {
        "CFBundleIdentifier": SHIM_BUNDLE_ID,
        "CFBundleName": SHIM_NAME,
        "CFBundleDisplayName": SHIM_NAME,
        "CFBundleExecutable": target.name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "NSBluetoothAlwaysUsageDescription": (
            "Matic Desk uses Bluetooth once, to pair this Mac with your Matic robot. "
            "After that it talks to the robot over Wi-Fi."
        ),
    }
    try:
        executable.parent.mkdir(parents=True, exist_ok=True)
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        if not executable.is_symlink() or os.readlink(executable) != str(target):
            executable.unlink(missing_ok=True)
            executable.symlink_to(target)
    except OSError as err:
        raise MaticDeskError("robot_error", f"Could not create the Bluetooth helper at {app}: {err}") from None
    return executable


def reexec_through_bluetooth_shim(argv: list[str]) -> None:
    """On macOS, restart `matic-desk <argv>` inside the helper bundle. Returns only
    when that is not needed (other platforms, or already inside the helper)."""
    if sys.platform != "darwin" or os.environ.get(SHIM_ENV):
        return
    executable = ensure_bluetooth_shim()
    env = {**os.environ, SHIM_ENV: "1", "PYTHONUNBUFFERED": "1"}
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(
        executable,
        [str(executable), "-c", _BOOTSTRAP, os.pathsep.join(site.getsitepackages()), *argv],
        env,
    )


def _run(cmd: list[str], timeout: float) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except subprocess.TimeoutExpired as err:
        out = err.stdout or ""
        return out.decode() if isinstance(out, bytes) else out
    except FileNotFoundError:
        raise MaticDeskError("offline", "dns-sd is not available; pairing needs macOS.") from None


def discover_robot() -> RobotInfo:
    """Find the robot through Bonjour using macOS's dns-sd."""
    browse = _run(["dns-sd", "-B", MDNS_SERVICE, "local."], timeout=4)
    instances = [
        " ".join(parts[6:])
        for line in browse.splitlines()
        if len(parts := line.split()) >= 7 and parts[1] == "Add"
    ]
    if not instances:
        raise MaticDeskError(
            "offline",
            "No Matic found on this network (nothing advertises _matic_hermes._tcp). Check the robot "
            "is on the same Wi-Fi, and that macOS allows Local Network access for your terminal.",
        )
    lookup = _run(["dns-sd", "-L", instances[0], MDNS_SERVICE, "local."], timeout=4)
    match = re.search(r"bot_information=(\S+)", lookup)
    info = decode_bot_information(match.group(1)) if match else None
    if info is None:
        raise MaticDeskError("robot_error", f"Found {instances[0]} but could not decode its Bonjour record.")
    return info


async def ble_request_credential(user_id: str, *, total_timeout: float = 300) -> HermesCredential:
    """Bond over Bluetooth and ask the robot to issue a credential.

    The robot starts the security exchange once something connects; macOS shows
    a "Bluetooth Pairing Request" asking for the six-digit code on the robot's
    screen. The token characteristic only appears on an authenticated link.
    """
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError

    found: dict[str, tuple[Any, Any]] = {}

    def on_advertisement(device: Any, advertisement: Any) -> None:
        uuids = {str(u).lower() for u in advertisement.service_uuids}
        name = (advertisement.local_name or device.name or "").lower()
        if MATIC_BLE_SERVICE in uuids or re.search(r"\bmatic", name):
            found[device.address] = (device, advertisement)

    print("Scanning for a Matic in Pairing mode over Bluetooth (up to 3 min)...", flush=True)
    deadline = time.monotonic() + 180
    try:
        async with BleakScanner(on_advertisement):
            while not found and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
    except BleakError as err:
        raise MaticDeskError(
            "robot_error",
            f"Bluetooth is unavailable to this process ({err}). In System Settings > Privacy & "
            f"Security > Bluetooth, allow \"{SHIM_NAME}\" (it may be listed as Python), then run "
            "pair again from Terminal.",
        ) from None
    if not found:
        raise MaticDeskError("robot_error", "No Matic is advertising. Turn on Pairing mode and run pair again.")
    device, advertisement = max(found.values(), key=lambda t: t[1].rssi or -127)
    print(f"Found {advertisement.local_name or device.name or 'a Matic'} (RSSI {advertisement.rssi} dBm)", flush=True)

    request = TokenRequest(user_id=user_id).SerializeToString()
    end = time.monotonic() + total_timeout
    attempt = 0
    last_problem = "no attempt completed"
    while time.monotonic() < end:
        attempt += 1
        print(
            f"Connecting over Bluetooth (attempt {attempt}). If a pairing dialog appears, "
            "type the six-digit code shown on the robot.",
            flush=True,
        )
        try:
            async with BleakClient(device, timeout=40) as client:
                characteristic = None
                for _ in range(12):  # about a minute for the pairing dialog
                    characteristic = client.services.get_characteristic(TOKEN_CHARACTERISTIC)
                    if characteristic is not None:
                        break
                    await asyncio.sleep(5)
                if characteristic is None:
                    last_problem = "token characteristic not visible yet (bond not complete?)"
                    continue
                payload = None
                for _ in range(12):
                    try:
                        await client.write_gatt_char(
                            characteristic,
                            request,
                            response="write-without-response" not in characteristic.properties,
                        )
                        payload = bytes(await client.read_gatt_char(characteristic))
                        break
                    except BleakError as err:  # usually "insufficient authentication" until the code is entered
                        last_problem = f"robot refused the request ({err})"
                        await asyncio.sleep(5)
                if payload is None:
                    continue
                credential = HermesCredential.from_message(BotToken.FromString(payload))
                if credential.app_id != user_id:
                    raise MaticDeskError("robot_error", "The robot returned a credential for a different user id.")
                return credential
        except (BleakError, TimeoutError, OSError) as err:
            last_problem = f"connection failed ({err})"
            await asyncio.sleep(3)
    raise MaticDeskError("robot_error", f"Bluetooth pairing did not complete: {last_problem}")


def _existing_app_id() -> str | None:
    value = read_robot_config().get("app_id")
    return value if isinstance(value, str) and value else None


async def pair() -> None:
    print(TERMINAL_NOTE, flush=True)
    info = await asyncio.to_thread(discover_robot)
    host = info.ip4_address or info.ip6_address
    hostname = bonjour_hostname(info.hostname)
    print(f"Robot: {info.name} ({hostname} -> {host}:{info.port})", flush=True)

    try:
        certificate = await async_fetch_peer_certificate(host, info.port)
        identity = validate_certificate(
            certificate, expected_hostname=info.hostname, expected_serial=info.serial_number
        )
    except MaticError as err:
        raise map_error(err) from err
    identifier, seconds = local_timezone()
    user_id = _existing_app_id() or new_hermes_user_id()

    def client(credential: HermesCredential | None = None) -> MaticHermesClient:
        return MaticHermesClient(
            host,
            info.port,
            hostname=hostname,
            serial_number=info.serial_number,
            certificate_fingerprint=identity.fingerprint,
            credential=credential,
            timezone_identifier=identifier,
            seconds_from_gmt=seconds,
        )

    credential: HermesCredential | None = None
    if info.network_auth:
        lan = client()
        try:
            credential = await lan.async_request_credential(user_id)
            print("Credential issued over the network.", flush=True)
        except PairingModeRequiredError:
            print("Network pairing refused; falling back to Bluetooth.", flush=True)
        except MaticError as err:
            raise map_error(err) from err
        finally:
            lan.close()
    if credential is None:
        if sys.platform == "darwin" and not os.environ.get(SHIM_ENV):
            raise MaticDeskError(
                "robot_error",
                "Bluetooth pairing must run inside the Matic Desk Pairing helper, or macOS stops "
                "the process. Run `matic-desk pair` from Terminal; it starts the helper itself.",
            )
        credential = await ble_request_credential(user_id)

    verify = client(credential)  # prove the credential works before saving anything
    try:
        state = await verify.async_get_state()
    except MaticError as err:
        raise map_error(err) from err
    finally:
        verify.close()

    # Merge, don't replace: other tools keep their own keys in robot.json.
    path = update_robot_config(
        {
            "host": host,
            "port": info.port,
            "hostname": info.hostname,
            "serial": info.serial_number,
            "name": info.name,
            "hardware_revision": info.hardware_revision,
            "fingerprint": identity.fingerprint,
            "app_id": user_id,
            "credential": credential.to_storage(),
            "paired_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    print(
        f"Paired. Saved to {path} (mode 0600). Robot is {state.activity.value}, "
        f"battery {state.battery_percentage}%.",
        flush=True,
    )
