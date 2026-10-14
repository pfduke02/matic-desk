"""JSON-lines bridge between the Swift menu bar app and MaticService (stdio).

stdout carries only protocol lines. Requests run concurrently; background
tasks push `status` and `bag` events. The process exits on stdin EOF.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from typing import Any, BinaryIO

from .config import setup_logging
from .errors import MaticDeskError, invalid
from .service import MaticService

log = logging.getLogger(__name__)

POLL_SECONDS = 30.0
INGEST_SECONDS = 300.0
RECONNECT_SECONDS = 5.0
RECONNECT_MAX_SECONDS = 120.0
DRAIN_SECONDS = 30.0
MAX_LINE = 1 << 20

Handler = Callable[[MaticService, dict[str, Any]], Awaitable[Any]]


async def _ping(service: MaticService, params: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True}


async def _map(service: MaticService, params: dict[str, Any]) -> dict[str, Any]:
    png = await service.map_png(route=params.get("route", True))
    return {"png_base64": base64.b64encode(png).decode("ascii")}


# method -> (handler, allowed params)
METHODS: dict[str, tuple[Handler, frozenset[str]]] = {
    "ping": (_ping, frozenset()),
    "status": (lambda s, p: s.status(), frozenset()),
    "settings": (lambda s, p: s.settings(), frozenset()),
    "rooms": (lambda s, p: s.rooms(), frozenset()),
    "history": (lambda s, p: s.history(**p), frozenset({"limit"})),
    "bag": (lambda s, p: s.bag_report(), frozenset()),
    "record_bag_swap": (lambda s, p: s.record_bag_swap(**p), frozenset({"at", "note"})),
    "clean": (
        lambda s, p: s.clean(**p),
        frozenset({"rooms", "whole_floor", "mode", "coverage", "ordered"}),
    ),
    "command": (lambda s, p: s.command(**p), frozenset({"name"})),
    "set_setting": (lambda s, p: s.set_setting(**p), frozenset({"setting", "value"})),
    "map": (_map, frozenset({"route"})),
    "name_room": (lambda s, p: s.name_room(**p), frozenset({"alias", "rooms"})),
    "remove_room_alias": (lambda s, p: s.remove_room_alias(**p), frozenset({"alias"})),
}
REQUIRED = {
    "command": {"name"},
    "set_setting": {"setting", "value"},
    "name_room": {"alias", "rooms"},
    "remove_room_alias": {"alias"},
}


async def dispatch(service: MaticService, request: Any) -> dict[str, Any]:
    """Run one request and return its response envelope (never raises)."""
    request_id = request.get("id") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict):
            raise invalid("A request must be a JSON object.")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise invalid("params must be a JSON object.")
        if method not in METHODS:
            raise invalid(f"Unknown method {method!r}. Methods: {', '.join(METHODS)}.")
        handler, allowed = METHODS[method]
        unknown = set(params) - allowed
        if unknown:
            raise invalid(f"Unknown parameter(s) for {method}: {', '.join(sorted(unknown))}.")
        missing = REQUIRED.get(method, set()) - set(params)
        if missing:
            raise invalid(f"Missing parameter(s) for {method}: {', '.join(sorted(missing))}.")
        result = await handler(service, params)
        return {"id": request_id, "ok": True, "result": result}
    except MaticDeskError as err:
        return {"id": request_id, "ok": False, "error": err.to_dict()}
    except Exception as err:
        log.exception("Request %r failed", request_id)
        error = MaticDeskError("robot_error", f"Internal error: {type(err).__name__}: {err}")
        return {"id": request_id, "ok": False, "error": error.to_dict()}


def status_key(status: dict[str, Any]) -> tuple[Any, ...]:
    """The fields whose change makes a `status` event worth sending."""
    return (
        status.get("connected"),
        status.get("activity"),
        status.get("battery"),
        status.get("current_room"),
        tuple(error.get("code") for error in status.get("errors") or []),
        status.get("bag_full"),
        status.get("bag_missing"),
    )


def bag_key(report: dict[str, Any]) -> str:
    """Bag report minus the parts that change with the clock alone."""
    stable = {k: v for k, v in report.items() if k not in ("days_since", "estimate")}
    stable["basis"] = (report.get("estimate") or {}).get("basis")
    return json.dumps(stable, sort_keys=True)


class Output:
    """Serialized writes of protocol lines to the real stdout."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        async with self._lock:
            if self.closed:
                return
            try:
                await asyncio.to_thread(self._write, data)
            except (BrokenPipeError, ValueError, OSError):
                self.closed = True
                log.warning("stdout closed; dropping output")

    def _write(self, data: bytes) -> None:
        self._stream.write(data)
        self._stream.flush()


class Bridge:
    def __init__(self, service: MaticService, output: Output) -> None:
        self.service = service
        self.output = output
        self._requests: set[asyncio.Task[None]] = set()
        self._last_status: tuple[Any, ...] | None = None
        self._last_activity: str | None = None
        self._last_bag: str | None = None
        self._last_stream_at: float | None = None
        self._ingest_now = asyncio.Event()

    # ---- requests

    def submit(self, line: bytes) -> None:
        task = asyncio.create_task(self._handle(line))
        self._requests.add(task)
        task.add_done_callback(self._requests.discard)

    async def _handle(self, line: bytes) -> None:
        try:
            request = json.loads(line)
        except ValueError:
            error = invalid("Each line must be one JSON object.").to_dict()
            await self.output.send({"id": None, "ok": False, "error": error})
            return
        await self.output.send(await dispatch(self.service, request))

    async def drain(self, timeout: float = DRAIN_SECONDS) -> None:
        """Let in-flight requests finish (bounded), then cancel any stragglers."""
        if self._requests:
            _, pending = await asyncio.wait(set(self._requests), timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- events

    async def publish_status(self, status: dict[str, Any]) -> None:
        key = status_key(status)
        left_cleaning = self._last_activity == "cleaning" and status.get("activity") != "cleaning"
        self._last_activity = status.get("activity")
        if key != self._last_status:
            self._last_status = key
            await self.output.send({"event": "status", "data": status})
        if left_cleaning:
            self._ingest_now.set()

    async def watch_stream(self) -> None:
        """Live state stream; reconnects with backoff when it drops."""
        delay = RECONNECT_SECONDS
        while True:
            try:
                async with aclosing(self.service.status_updates()) as updates:
                    async for status in updates:
                        self._last_stream_at = time.monotonic()
                        delay = RECONNECT_SECONDS
                        await self.publish_status(status)
                log.info("State stream ended; reconnecting")
            except MaticDeskError as err:
                log.info("State stream unavailable: %s", err)
            except Exception:
                log.exception("State stream failed")
            self._last_stream_at = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    async def poll_status(self) -> None:
        """Fallback poll whenever the stream has been quiet for POLL_SECONDS."""
        while True:
            quiet_since = self._last_stream_at
            if quiet_since is None or time.monotonic() - quiet_since >= POLL_SECONDS:
                try:
                    await self.publish_status(await self.service.status())
                except Exception:
                    log.exception("Status poll failed")
            await asyncio.sleep(POLL_SECONDS)

    async def ingest_history(self) -> None:
        """Every INGEST_SECONDS, and right after a run ends: refresh the bag report."""
        while True:
            self._ingest_now.clear()  # a run ending during this ingest triggers another
            try:
                report = await self.service.bag_report()
                key = bag_key(report)
                if key != self._last_bag:
                    self._last_bag = key
                    await self.output.send({"event": "bag", "data": report})
            except Exception:
                log.exception("Background history ingest failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._ingest_now.wait(), INGEST_SECONDS)


async def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE)
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (ValueError, OSError):
        # stdin is a regular file: fall back to a reader thread.
        def pump() -> None:
            for line in sys.stdin.buffer:
                loop.call_soon_threadsafe(reader.feed_data, line)
            loop.call_soon_threadsafe(reader.feed_eof)

        loop.run_in_executor(None, pump)
    return reader


async def serve(
    service: MaticService,
    reader: asyncio.StreamReader,
    output: Output,
    *,
    background: bool = True,
) -> None:
    """Handle requests from `reader` until EOF, then finish in-flight work and return."""
    bridge = Bridge(service, output)
    tasks = (
        [asyncio.create_task(job()) for job in (bridge.watch_stream, bridge.poll_status, bridge.ingest_history)]
        if background
        else []
    )
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # line longer than MAX_LINE
                await output.send({"id": None, "ok": False, "error": invalid("Request too long.").to_dict()})
                continue
            if not line:
                break
            if line.strip():
                bridge.submit(line)
        log.info("stdin closed; shutting down")
        await bridge.drain()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()


async def _run() -> None:
    # Keep the real stdout for protocol lines only: point fd 1 and sys.stdout at
    # stderr so a stray print (ours or a library's) cannot corrupt the protocol.
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    output = Output(os.fdopen(protocol_fd, "wb", buffering=0))
    await serve(MaticService(), await _stdin_reader(), output)


def main() -> None:
    setup_logging()
    log.info("matic-desk-bridge starting (pid %d)", os.getpid())
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    log.info("matic-desk-bridge stopped")


if __name__ == "__main__":
    main()
