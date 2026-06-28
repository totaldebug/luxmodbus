"""Async transports for talking to a LuxPower dongle.

Two interchangeable transports sit behind one interface so the layers above
never know which is active:

* :class:`ClientTransport` — Home Assistant dials the dongle
  (``asyncio.open_connection``). The default; it owns the connection and
  reconnects with exponential backoff when it drops.
* :class:`ServerTransport` — the dongle dials us (``asyncio.start_server``); we
  send on whichever connection the dongle most recently opened.

Both deliver complete frames (reassembled from the TCP stream via
:func:`luxmodbus.protocol.extract_frames`) to registered callbacks, and expose
``send`` / ``connect`` / ``close``. There is no Home Assistant dependency.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable

from luxmodbus.protocol import extract_frames

__all__ = [
    "DEFAULT_PORT",
    "ClientTransport",
    "ServerTransport",
    "Transport",
    "TransportConnectError",
    "TransportError",
    "TransportNotConnectedError",
]

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8000
_READ_SIZE = 4096

FrameCallback = Callable[[bytes], None]
StateCallback = Callable[[bool], None]


class TransportError(Exception):
    """Base class for transport errors."""


class TransportConnectError(TransportError):
    """Raised when an initial connection cannot be established."""


class TransportNotConnectedError(TransportError):
    """Raised when sending while no connection is available."""


class Transport(ABC):
    """Common callback handling and interface for the two transports."""

    def __init__(self) -> None:
        self._frame_callbacks: list[FrameCallback] = []
        self._state_callbacks: list[StateCallback] = []

    def on_frame(self, callback: FrameCallback) -> Callable[[], None]:
        """Register a callback for each complete frame; returns an unsubscribe."""
        self._frame_callbacks.append(callback)
        return lambda: _safe_remove(self._frame_callbacks, callback)

    def on_state(self, callback: StateCallback) -> Callable[[], None]:
        """Register a callback for connection-state changes; returns an unsubscribe."""
        self._state_callbacks.append(callback)
        return lambda: _safe_remove(self._state_callbacks, callback)

    def _emit_frame(self, frame: bytes) -> None:
        """Deliver a frame to every frame callback."""
        for callback in list(self._frame_callbacks):
            callback(frame)

    def _emit_state(self, connected: bool) -> None:
        """Deliver a connection-state change to every state callback."""
        for callback in list(self._state_callbacks):
            callback(connected)

    @abstractmethod
    async def connect(self) -> None:
        """Establish the transport (raising :class:`TransportConnectError` on failure)."""

    @abstractmethod
    async def send(self, frame: bytes) -> None:
        """Send one already-encoded frame."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the transport and stop reconnecting."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether a connection is currently established."""


class ClientTransport(Transport):
    """Connects out to the dongle and keeps the connection alive."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout: float = 10.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        backoff_reset_after: float = 30.0,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._backoff_reset_after = backoff_reset_after
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def is_connected(self) -> bool:
        """Whether the outbound connection is currently open."""
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open the first connection, then maintain it in the background."""
        self._closing = False
        reader, writer = await self._open()
        self._writer = writer
        self._emit_state(True)
        self._task = asyncio.create_task(self._maintain(reader))

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Make a single connection attempt, raising on failure."""
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._connect_timeout,
            )
        except (OSError, TimeoutError) as err:
            raise TransportConnectError(f"cannot connect to {self._host}:{self._port}") from err

    async def _maintain(self, reader: asyncio.StreamReader) -> None:
        """Read frames; on disconnect, reconnect with exponential backoff."""
        loop = asyncio.get_running_loop()
        backoff = self._backoff_initial
        while not self._closing:
            started = loop.time()
            try:
                await self._read_loop(reader)
            except (OSError, asyncio.IncompleteReadError) as err:
                _LOGGER.debug("read loop ended: %s", err)
            if self._writer is not None:
                self._writer = None
                self._emit_state(False)
            if self._closing:
                return
            # A connection that stayed up a while is treated as healthy, so the
            # next blip reconnects promptly rather than at a grown backoff.
            if loop.time() - started >= self._backoff_reset_after:
                backoff = self._backoff_initial
            reader = await self._reconnect(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def _reconnect(self, backoff: float) -> asyncio.StreamReader:
        """Sleep, then keep retrying the connection until it succeeds or we close."""
        while not self._closing:
            await asyncio.sleep(backoff)
            try:
                reader, writer = await self._open()
            except TransportConnectError:
                backoff = min(backoff * 2, self._backoff_max)
                continue
            self._writer = writer
            self._emit_state(True)
            return reader
        return asyncio.StreamReader()  # unreachable once closing; keeps typing happy

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read from the stream and emit complete frames until EOF."""
        buffer = b""
        while not self._closing:
            data = await reader.read(_READ_SIZE)
            if not data:
                return  # EOF
            frames, buffer = extract_frames(buffer + data)
            for frame in frames:
                self._emit_frame(frame)

    async def send(self, frame: bytes) -> None:
        """Write an encoded frame to the dongle."""
        if not self.is_connected or self._writer is None:
            raise TransportNotConnectedError("client transport is not connected")
        self._writer.write(frame)
        await self._writer.drain()

    async def close(self) -> None:
        """Stop reconnecting and close the connection."""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await _close_writer(self._writer)
        self._writer = None


class ServerTransport(Transport):
    """Listens for the dongle to connect and serves whichever connection is newest."""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def is_connected(self) -> bool:
        """Whether a dongle connection is currently open."""
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Start listening for the dongle to connect."""
        try:
            self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        except OSError as err:
            raise TransportConnectError(f"cannot listen on {self._host}:{self._port}") from err

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Track the connection and emit frames read from it."""
        await _close_writer(self._writer)
        self._writer = writer
        self._emit_state(True)
        buffer = b""
        try:
            while True:
                data = await reader.read(_READ_SIZE)
                if not data:
                    break
                frames, buffer = extract_frames(buffer + data)
                for frame in frames:
                    self._emit_frame(frame)
        except OSError as err:
            _LOGGER.debug("server read loop ended: %s", err)
        finally:
            if self._writer is writer:
                self._writer = None
                self._emit_state(False)
            await _close_writer(writer)

    async def send(self, frame: bytes) -> None:
        """Write an encoded frame to the connected dongle."""
        if not self.is_connected or self._writer is None:
            raise TransportNotConnectedError("no dongle connection")
        self._writer.write(frame)
        await self._writer.drain()

    async def close(self) -> None:
        """Stop listening and drop any active connection."""
        await _close_writer(self._writer)
        self._writer = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def _safe_remove(items: list, item: object) -> None:
    """Remove ``item`` from ``items`` if present (idempotent unsubscribe)."""
    if item in items:
        items.remove(item)


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    """Close a stream writer, ignoring errors raised during teardown."""
    if writer is None or writer.is_closing():
        return
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
