"""One long-lived, lazily opened robot connection per process.

`MaticRobot` wraps the vendored Hermes client: it opens the connection on first
use, reopens it after a transport failure (reads retry once; writes are never
resent once attempted) and turns every client exception into `MaticDeskError`.
Concurrent callers share one in-flight dial, and after a failed dial reads fail
fast for a short cool-down instead of queueing up more ~10 s dials.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import datetime, timedelta
from typing import Any, Protocol, TypeVar

from .errors import MaticDeskError
from .vendor.hermes_client.api import MaticHermesClient
from .vendor.hermes_client.api import _decode_cleaning_session as decode_cleaning_session
from .vendor.hermes_client.auth import HermesCredential
from .vendor.hermes_client.commands import CleaningMode, CoverageSetting, UserCommand
from .vendor.hermes_client.exceptions import (
    AuthenticationRequiredError,
    CannotConnectError,
    CertificateMismatchError,
    EndpointUnsupportedError,
    InvalidRobotCertificateError,
    MaticError,
)
from .vendor.hermes_client.models import (
    CleaningSessionRecord,
    FloorPlan,
    RobotOperationalState,
    RobotPose,
    RobotTelemetry,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

LOCAL_NETWORK_HINT = (
    "Check that the robot is on and on this network. macOS may also be blocking "
    "Local Network access for this app: System Settings > Privacy & Security > "
    "Local Network."
)
_BLOCKED_ERRNOS = {errno.EHOSTUNREACH, errno.EPERM}
_BLOCKED_TEXT = ("no route to host", "operation not permitted", "[errno 65]", "[errno 1]")
_RETRYABLE = (CannotConnectError, OSError, TimeoutError)

OFFLINE_COOLDOWN_SECONDS = 20.0
HISTORY_COLLECTION = "coverage_session_history"
# The history snapshot has no end marker: it ends when the robot goes quiet. The
# vendored default (0.25 s) cuts it short when the robot is busy, silently
# dropping the newest runs, so wait longer for the next record.
HISTORY_IDLE_SECONDS = 2.0


class Robot(Protocol):
    """What `MaticService` needs from a robot; tests provide a fake."""

    name: str | None

    async def get_state(self) -> RobotOperationalState: ...
    async def get_telemetry(self) -> RobotTelemetry: ...
    async def get_session_records(self) -> tuple[CleaningSessionRecord, ...]: ...
    async def get_floor_plan(self) -> FloorPlan: ...
    async def get_pose(self) -> RobotPose | None: ...
    async def get_route(self, plan: FloorPlan, wait: float = 4.0) -> tuple[tuple[float, float], ...]: ...
    async def start_coverage(
        self,
        plan: FloorPlan,
        region_ids: list[str],
        cleaning_mode: CleaningMode,
        coverage_setting: CoverageSetting,
        ordered: bool,
    ) -> None: ...
    async def send_command(self, command: UserCommand) -> None: ...
    async def set_water_flow(self, value: float) -> None: ...
    async def set_deep_mop(self, enabled: bool) -> None: ...
    async def set_binary_setting(self, setting: str, enabled: bool) -> None: ...
    def subscribe_state(self) -> AsyncIterator[RobotOperationalState]: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------- error mapping


def looks_blocked(err: BaseException) -> bool:
    """True for EHOSTUNREACH / EPERM-style failures (macOS Local Network denial)."""
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in _BLOCKED_ERRNOS:
            return True
        text = str(current).lower()
        if any(fragment in text for fragment in _BLOCKED_TEXT):
            return True
        current = current.__cause__ or current.__context__
    return False


def map_error(err: BaseException) -> MaticDeskError:
    """Translate a vendored-client or transport exception into the contract's types."""
    if isinstance(err, MaticDeskError):
        return err
    if isinstance(err, (CertificateMismatchError, InvalidRobotCertificateError)):
        return MaticDeskError(
            "offline",
            "The robot's TLS identity does not match the paired robot. If it was reset "
            "or replaced, run `matic-desk pair` in Terminal again.",
        )
    if isinstance(err, AuthenticationRequiredError):
        return MaticDeskError(
            "robot_error",
            "The robot rejected the saved credential. Run `matic-desk pair` in Terminal again.",
        )
    if isinstance(err, EndpointUnsupportedError):
        return MaticDeskError("robot_error", str(err) or "This firmware does not support that request.")
    if isinstance(err, CannotConnectError) and "did not confirm" in str(err):
        return MaticDeskError("robot_error", str(err))
    if isinstance(err, _RETRYABLE):
        detail = str(err) or type(err).__name__
        message = f"Can't reach the robot ({detail})."
        if looks_blocked(err):
            message += " " + LOCAL_NETWORK_HINT
        return MaticDeskError("offline", message)
    if isinstance(err, ValueError):
        return MaticDeskError("invalid_request", str(err))
    if isinstance(err, MaticError):
        return MaticDeskError("robot_error", str(err) or type(err).__name__)
    return MaticDeskError("robot_error", f"Unexpected error: {type(err).__name__}: {err}")


# ------------------------------------------------------------------ connection


def local_timezone() -> tuple[str, int]:
    offset = datetime.now().astimezone().utcoffset() or timedelta()
    try:
        identifier = os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        identifier = "UTC"
    return identifier, int(offset.total_seconds())


def bonjour_hostname(hostname: str) -> str:
    """robot.json stores the bare name; Bonjour resolution needs `.local`."""
    hostname = hostname.strip().rstrip(".")
    return hostname if "." in hostname else f"{hostname}.local"


def make_client(config: dict[str, Any]) -> MaticHermesClient:
    try:
        credential = HermesCredential.from_storage(config["credential"])
    except (KeyError, ValueError):
        raise MaticDeskError(
            "not_paired", "The saved robot credential is invalid. Run `matic-desk pair` in Terminal again."
        ) from None
    identifier, seconds = local_timezone()
    return MaticHermesClient(
        str(config["host"]),
        int(config["port"]),
        hostname=bonjour_hostname(str(config["hostname"])),
        serial_number=config.get("serial"),
        certificate_fingerprint=config.get("fingerprint"),
        credential=credential,
        timezone_identifier=identifier,
        seconds_from_gmt=seconds,
    )


class MaticRobot:
    """The real robot, reached over the LAN with the paired credential."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client_factory: Callable[[dict[str, Any]], Any] = make_client,
        offline_cooldown: float = OFFLINE_COOLDOWN_SECONDS,
    ) -> None:
        self.name: str | None = config.get("name") or config.get("hostname")
        self._config = config
        self._client_factory = client_factory
        self._client: Any = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._history_lock = asyncio.Lock()
        self._dial: asyncio.Task[None] | None = None
        self._offline: MaticDeskError | None = None
        self._offline_until = 0.0
        self._offline_cooldown = offline_cooldown

    async def _connected(self) -> tuple[Any, int]:
        """Open (or reuse) the channel. Concurrent callers wait on the same dial."""
        async with self._lock:
            if self._offline is not None and time.monotonic() < self._offline_until:
                raise MaticDeskError(self._offline.type, self._offline.message)
            if self._client is None:
                self._client = self._client_factory(self._config)
            client, generation = self._client, self._generation
            if self._dial is None or self._dial.done():
                self._dial = asyncio.create_task(self._dial_once(client))
                self._dial.add_done_callback(_consume_exception)
            dial = self._dial
        try:
            await asyncio.shield(dial)  # a cancelled caller does not cancel the shared dial
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if dial.cancelled() and not (task and task.cancelling()):
                # close() stopped the dial; this caller itself was not cancelled.
                raise MaticDeskError("offline", "The robot connection was closed.") from None
            raise
        return client, generation

    async def _dial_once(self, client: Any) -> None:
        try:
            await client.async_connect()
        except Exception as err:
            mapped = map_error(err)
            if mapped.type == "offline":
                self._offline = mapped
                self._offline_until = time.monotonic() + self._offline_cooldown
            raise
        self._offline = None

    async def _reset(self, generation: int) -> None:
        """Drop the channel unless a concurrent caller already replaced it."""
        async with self._lock:
            if self._client is not None and self._generation == generation:
                self._client.close()
                self._generation += 1

    async def _read(self, op: Callable[[Any], Awaitable[T]]) -> T:
        """Idempotent read. If an open channel turns out to be stale, reopen and retry once.

        A failed connect is not retried: the client already tried every address.
        """
        for attempt in (1, 2):
            generation = self._generation
            connected = False
            try:
                client, generation = await self._connected()
                connected = True
                return await op(client)
            except Exception as err:
                if isinstance(err, _RETRYABLE):
                    await self._reset(generation)
                if attempt == 2 or not connected or not isinstance(err, _RETRYABLE):
                    raise map_error(err) from err
                log.info("Robot read failed (%s); reconnecting", type(err).__name__)
        raise AssertionError("unreachable")

    async def _write(self, op: Callable[[Any], Awaitable[T]]) -> T:
        """Prove the channel with a state read (which may reconnect), then send exactly once."""
        client = await self._read(_alive)
        generation = self._generation
        try:
            return await op(client)
        except Exception as err:
            if isinstance(err, _RETRYABLE):
                await self._reset(generation)
            raise map_error(err) from err

    # ---- reads

    async def get_state(self) -> RobotOperationalState:
        return await self._read(lambda c: c.async_get_state())

    async def get_telemetry(self) -> RobotTelemetry:
        async with self._history_lock:  # telemetry streams the history collection too
            return await self._read(lambda c: c.async_get_telemetry())

    async def get_session_records(self) -> tuple[CleaningSessionRecord, ...]:
        """The robot's retained runs. Serialized: overlapping reads truncate each other."""
        async with self._history_lock:
            return await self._read(_read_session_records)

    async def get_floor_plan(self) -> FloorPlan:
        return await self._read(lambda c: c.async_get_floor_plan())

    async def get_pose(self) -> RobotPose | None:
        try:
            return await self._read(lambda c: c.async_get_pose())
        except MaticDeskError:
            return None

    async def get_route(self, plan: FloorPlan, wait: float = 4.0) -> tuple[tuple[float, float], ...]:
        """First non-empty route snapshot of the active mission, or () after `wait` s."""
        try:
            client, _ = await self._connected()
            stream = client.async_subscribe_approximate_trajectory(plan)
            async with asyncio.timeout(wait), aclosing(stream):
                async for trajectory in stream:
                    if trajectory.points:
                        return trajectory.points
        except (TimeoutError, MaticError, MaticDeskError, OSError):
            pass
        return ()

    async def subscribe_state(self) -> AsyncIterator[RobotOperationalState]:
        try:
            client, _ = await self._connected()
            async with aclosing(client.async_subscribe_state()) as stream:
                async for state in stream:
                    yield state
        except Exception as err:
            raise map_error(err) from err

    # ---- writes

    async def start_coverage(
        self,
        plan: FloorPlan,
        region_ids: list[str],
        cleaning_mode: CleaningMode,
        coverage_setting: CoverageSetting,
        ordered: bool,
    ) -> None:
        await self._write(
            lambda c: c.async_start_coverage(
                plan,
                region_ids,
                cleaning_mode=cleaning_mode,
                coverage_setting=coverage_setting,
                ordered=ordered,
            )
        )

    async def send_command(self, command: UserCommand) -> None:
        await self._write(lambda c: c.async_send_user_command(command))

    async def set_water_flow(self, value: float) -> None:
        await self._write(lambda c: c.async_set_water_flow(value))

    async def set_deep_mop(self, enabled: bool) -> None:
        await self._write(lambda c: c.async_set_deep_mop(enabled))

    async def set_binary_setting(self, setting: str, enabled: bool) -> None:
        await self._write(lambda c: c.async_set_binary_setting(setting, enabled))

    async def close(self) -> None:
        async with self._lock:
            if self._dial is not None and not self._dial.done():
                self._dial.cancel()
            self._dial = None
            if self._client is not None:
                self._client.close()
                self._client = None


async def _alive(client: Any) -> Any:
    await client.async_get_state()
    return client


async def _read_session_records(client: Any) -> tuple[CleaningSessionRecord, ...]:
    """Like the vendored `async_get_cleaning_session_records`, with a tolerant idle timeout."""
    entries = await client.async_get_tracked_collection_entries(
        HISTORY_COLLECTION, limit=64, idle_timeout=HISTORY_IDLE_SECONDS
    )
    return tuple(
        CleaningSessionRecord(entry.key, session)
        for entry in entries
        if entry.key and (session := decode_cleaning_session(entry.value)) is not None
    )


def _consume_exception(task: asyncio.Task[None]) -> None:
    """Mark a shared dial's failure as seen; every waiter already got it through `shield`."""
    if not task.cancelled():
        task.exception()
