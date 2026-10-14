import asyncio
import errno
import time
from uuid import uuid4

import pytest
from conftest import make_state

from matic_desk import robot as robot_module
from matic_desk.errors import MaticDeskError
from matic_desk.robot import HISTORY_IDLE_SECONDS, MaticRobot, bonjour_hostname, make_client, map_error
from matic_desk.vendor.hermes_client.auth import HermesCredential
from matic_desk.vendor.hermes_client.commands import UserCommand
from matic_desk.vendor.hermes_client.exceptions import (
    AuthenticationRequiredError,
    CannotConnectError,
    CertificateMismatchError,
    EndpointUnsupportedError,
    MaticError,
)
from matic_desk.vendor.hermes_client.proto.hermes_auth_pb2 import TokenRequest

CONFIG = {"name": "matic-abcd-efgh", "host": "192.0.2.10", "port": 16320, "hostname": "matic-abcd-efgh",
          "serial": "TEST-SERIAL", "fingerprint": "00" * 32}


def test_bonjour_hostname():
    assert bonjour_hostname("matic-abcd-efgh") == "matic-abcd-efgh.local"
    assert bonjour_hostname("matic-abcd-efgh.local.") == "matic-abcd-efgh.local"


def test_make_client_appends_local_and_rejects_bad_credentials():
    credential = HermesCredential(b"\x01" * 32, TokenRequest(user_id=str(uuid4())).SerializeToString())
    client = make_client({**CONFIG, "credential": credential.to_storage()})
    assert client._hostname == "matic-abcd-efgh.local"
    client.close()
    with pytest.raises(MaticDeskError) as caught:
        make_client({**CONFIG, "credential": "not a credential"})
    assert caught.value.type == "not_paired"


def test_local_network_hint_for_no_route_to_host():
    error = map_error(CannotConnectError("[Errno 65] No route to host"))
    assert error.type == "offline" and "Local Network" in error.message


def test_local_network_hint_for_chained_eperm():
    try:
        try:
            raise OSError(errno.EPERM, "Operation not permitted")
        except OSError as inner:
            raise CannotConnectError("Hermes info connection failed") from inner
    except CannotConnectError as outer:
        error = map_error(outer)
    assert error.type == "offline" and "Local Network" in error.message


def test_plain_timeout_has_no_hint():
    error = map_error(CannotConnectError("Hermes connection timed out"))
    assert error.type == "offline" and "Local Network" not in error.message


@pytest.mark.parametrize(
    "exception, kind",
    [
        (CertificateMismatchError("changed"), "offline"),
        (AuthenticationRequiredError("no"), "robot_error"),
        (EndpointUnsupportedError("nope"), "robot_error"),
        (CannotConnectError("The robot did not confirm the requested setting"), "robot_error"),
        (MaticError("odd"), "robot_error"),
        (TimeoutError(), "offline"),
        (ValueError("Water flow must be between 0.5 and 2.0"), "invalid_request"),
    ],
)
def test_error_mapping(exception, kind):
    assert map_error(exception).type == kind


class FakeClient:
    def __init__(self, read_failures: int = 0, read_error: Exception | None = None) -> None:
        self.read_failures = read_failures
        self.read_error = read_error or CannotConnectError("Hermes state connection failed")
        self.connects = self.closes = self.sends = 0

    async def async_connect(self) -> None:
        self.connects += 1

    def close(self) -> None:
        self.closes += 1

    async def async_get_state(self):
        if self.read_failures:
            self.read_failures -= 1
            raise self.read_error
        return make_state()

    async def async_send_user_command(self, command) -> None:
        self.sends += 1
        raise CannotConnectError("Hermes user_command connection failed")


async def test_read_reconnects_and_retries_once():
    client = FakeClient(read_failures=1)
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    assert (await robot.get_state()).activity.value == "docked"
    assert client.closes == 1


async def test_read_gives_up_after_the_retry():
    client = FakeClient(read_failures=2)
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    with pytest.raises(MaticDeskError) as caught:
        await robot.get_state()
    assert caught.value.type == "offline"


async def test_identity_errors_are_not_retried():
    client = FakeClient(read_failures=5, read_error=CertificateMismatchError("changed"))
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    with pytest.raises(MaticDeskError):
        await robot.get_state()
    assert client.read_failures == 4 and client.closes == 0


async def test_writes_are_never_resent():
    client = FakeClient()
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    with pytest.raises(MaticDeskError) as caught:
        await robot.send_command(UserCommand.PAUSE)
    assert caught.value.type == "offline"
    assert client.sends == 1 and client.closes == 1


async def test_close_drops_the_client():
    client = FakeClient()
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    await robot.get_state()
    await robot.close()
    assert client.closes == 1 and robot._client is None


async def test_failed_connect_is_not_redialled_immediately():
    client = FakeClient()

    async def unreachable() -> None:
        client.connects += 1
        raise CannotConnectError("[Errno 65] No route to host")

    client.async_connect = unreachable
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    with pytest.raises(MaticDeskError) as caught:
        await robot.get_state()
    assert caught.value.type == "offline" and "Local Network" in caught.value.message
    assert client.connects == 1


async def test_unreachable_robot_shares_one_dial_then_fails_fast():
    """Callers used to queue one ~10 s dial each, so the app timed out before seeing `offline`."""
    client = FakeClient()
    dial_lock = asyncio.Lock()  # the vendored client also dials one at a time

    async def slow_unreachable() -> None:
        async with dial_lock:
            client.connects += 1
            await asyncio.sleep(0.2)
            raise CannotConnectError("Hermes connection timed out")

    client.async_connect = slow_unreachable
    robot = MaticRobot(CONFIG, client_factory=lambda config: client, offline_cooldown=0.5)
    started = time.monotonic()
    results = await asyncio.gather(*(robot.get_state() for _ in range(6)), return_exceptions=True)
    assert time.monotonic() - started < 0.4
    assert client.connects == 1
    assert {type(r) for r in results} == {MaticDeskError} and {r.type for r in results} == {"offline"}

    with pytest.raises(MaticDeskError) as caught:  # inside the cool-down: no new dial
        await robot.get_state()
    assert caught.value.type == "offline" and client.connects == 1
    assert await robot.get_route(object(), wait=0.1) == ()

    await asyncio.sleep(0.55)
    client.async_connect = FakeClient().async_connect  # robot is back
    assert (await robot.get_state()).activity.value == "docked"


async def test_close_during_a_dial_fails_waiters_cleanly():
    client = FakeClient()
    started = asyncio.Event()

    async def hanging() -> None:
        started.set()
        await asyncio.sleep(10)

    client.async_connect = hanging
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    waiters = [asyncio.create_task(robot.get_state()) for _ in range(2)]
    await started.wait()
    await robot.close()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert [getattr(r, "type", r) for r in results] == ["offline", "offline"]


async def test_history_reads_are_serialized_and_patient(monkeypatch):
    class HistoryClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.active = self.most_active = 0
            self.idle_timeouts: list[float] = []

        async def async_get_tracked_collection_entries(self, name, *, limit, idle_timeout):
            assert name == "coverage_session_history"
            self.idle_timeouts.append(idle_timeout)
            self.active += 1
            self.most_active = max(self.most_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return tuple(type("Entry", (), {"key": bytes([i]), "value": b"x"})() for i in range(1, 4))

        async def async_get_telemetry(self):
            self.active += 1
            self.most_active = max(self.most_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return "telemetry"

    monkeypatch.setattr(robot_module, "decode_cleaning_session", lambda payload: "session")
    client = HistoryClient()
    robot = MaticRobot(CONFIG, client_factory=lambda config: client)
    results = await asyncio.gather(
        robot.get_session_records(), robot.get_telemetry(), robot.get_session_records()
    )
    assert client.most_active == 1
    assert client.idle_timeouts == [HISTORY_IDLE_SECONDS] * 2 and HISTORY_IDLE_SECONDS >= 1
    assert [record.key for record in results[0]] == [b"\x01", b"\x02", b"\x03"]
