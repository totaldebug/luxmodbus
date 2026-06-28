"""Tests for luxmodbus.transport over real loopback sockets."""

from __future__ import annotations

import asyncio
import struct

import pytest

from luxmodbus.protocol import DeviceFunction, Frame, TcpFunction
from luxmodbus.transport import (
    ClientTransport,
    ServerTransport,
    TransportConnectError,
    TransportNotConnectedError,
)

INVERTER = b"1234567890"
DONGLE = b"BA12345678"


def a_frame(register: int) -> bytes:
    """Build a small encoded frame for transport tests."""
    data = struct.pack("<BB", 0, DeviceFunction.WRITE_SINGLE) + INVERTER + struct.pack("<HH", register, 0)
    return Frame(tcp_function=TcpFunction.TRANSLATED_DATA, dongle_serial=DONGLE, data=data).encode()


async def _free_port() -> int:
    """Bind to an ephemeral port and return it."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll until predicate() is truthy or the timeout elapses."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


# --- ClientTransport ---------------------------------------------------------


async def test_client_connect_failure_raises():
    transport = ClientTransport("127.0.0.1", await _free_port(), connect_timeout=0.5)
    with pytest.raises(TransportConnectError):
        await transport.connect()


async def test_client_receives_frames_from_server():
    received: list[bytes] = []
    port = await _free_port()
    sent_frame = a_frame(21)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(sent_frame)
        await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    # High backoff so a single server-side drop does not reconnect mid-test.
    transport = ClientTransport("127.0.0.1", port, backoff_initial=30)
    transport.on_frame(received.append)
    try:
        await transport.connect()
        assert transport.is_connected
        await _wait_for(lambda: received == [sent_frame])
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


async def test_client_send_reaches_server():
    got: list[bytes] = []
    port = await _free_port()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        got.append(data)

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    transport = ClientTransport("127.0.0.1", port, backoff_initial=30)
    try:
        await transport.connect()
        await transport.send(a_frame(5))
        await _wait_for(lambda: got and got[0] == a_frame(5))
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


async def test_client_send_when_disconnected_raises():
    transport = ClientTransport("127.0.0.1", await _free_port())
    with pytest.raises(TransportNotConnectedError):
        await transport.send(a_frame(1))


async def test_client_reconnects_after_drop():
    states: list[bool] = []
    port = await _free_port()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()  # immediately drop the client

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    transport = ClientTransport("127.0.0.1", port, backoff_initial=0.05, backoff_max=0.1)
    transport.on_state(states.append)
    try:
        await transport.connect()
        # Expect at least one drop (False) followed by a reconnect (True).
        await _wait_for(lambda: states.count(False) >= 1 and states.count(True) >= 2)
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


# --- ServerTransport ---------------------------------------------------------


async def test_server_receives_and_sends():
    received: list[bytes] = []
    port = await _free_port()
    transport = ServerTransport("127.0.0.1", port)
    transport.on_frame(received.append)
    await transport.connect()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _wait_for(lambda: transport.is_connected)

        writer.write(a_frame(2))
        await writer.drain()
        await _wait_for(lambda: received == [a_frame(2)])

        await transport.send(a_frame(8))
        echoed = await asyncio.wait_for(reader.readexactly(len(a_frame(8))), timeout=2.0)
        assert echoed == a_frame(8)

        writer.close()
        await writer.wait_closed()
    finally:
        await transport.close()


async def test_server_send_without_client_raises():
    transport = ServerTransport("127.0.0.1", await _free_port())
    await transport.connect()
    try:
        with pytest.raises(TransportNotConnectedError):
            await transport.send(a_frame(1))
    finally:
        await transport.close()
