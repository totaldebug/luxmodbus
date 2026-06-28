"""Tests for luxmodbus.protocol.

Ground-truth is two-tier: these synthetic tests assert self-consistent
round-trips, a canonical CRC known-answer, and the documented example's length
fields. Real captured packets dropped into ``tests/fixtures/*.bin`` are picked
up automatically by ``test_captured_frames_round_trip``.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from luxmodbus.protocol import (
    HEADER_LEN,
    PREFIX,
    CrcError,
    DataFrame,
    DeviceFunction,
    Frame,
    PrefixError,
    TcpFunction,
    TruncatedFrameError,
    crc16,
)

DONGLE = b"BA12345678"
INVERTER = b"1234567890"


def make_write_single() -> Frame:
    data = DataFrame(
        action=0,
        device_function=DeviceFunction.WRITE_SINGLE,
        inverter_serial=INVERTER,
        register=21,
        value=struct.pack("<H", 0x00FF),
    ).encode()
    return Frame(
        tcp_function=TcpFunction.TRANSLATED_DATA,
        dongle_serial=DONGLE,
        data=data,
        protocol=2,
    )


# --- CRC ---------------------------------------------------------------------


def test_crc16_known_answer():
    # Canonical Modbus CRC-16 check value for the ASCII string "123456789".
    assert crc16(b"123456789") == 0x4B37


def test_crc16_empty():
    assert crc16(b"") == 0xFFFF


# --- Outer frame round-trip --------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"\x00\x06" + INVERTER + b"\x15\x00\xff\x00",
        b"",
        bytes(range(40)),
    ],
)
def test_frame_round_trip(data: bytes):
    frame = Frame(
        tcp_function=TcpFunction.TRANSLATED_DATA,
        dongle_serial=DONGLE,
        data=data,
        protocol=2,
    )
    assert Frame.decode(frame.encode()) == frame


def test_frame_byte_layout_matches_spec_example():
    packet = make_write_single().encode()
    # Documented write-single example: frame_length 32, data_length 18, total 38.
    assert len(packet) == 38
    assert packet[0:2] == PREFIX
    assert struct.unpack_from("<H", packet, 2)[0] == 2  # protocol
    assert struct.unpack_from("<H", packet, 4)[0] == 32  # frame_length
    assert packet[6] == 0x01  # reserved
    assert packet[7] == TcpFunction.TRANSLATED_DATA
    assert packet[8:18] == DONGLE
    assert struct.unpack_from("<H", packet, 18)[0] == 18  # data_length
    assert struct.unpack_from("<H", packet, len(packet) - 2)[0] == crc16(packet[HEADER_LEN : len(packet) - 2])


# --- Inner data frame --------------------------------------------------------


def test_data_frame_write_single_layout():
    df = DataFrame(
        action=0,
        device_function=DeviceFunction.WRITE_SINGLE,
        inverter_serial=INVERTER,
        register=21,
        value=struct.pack("<H", 0x00FF),
    )
    encoded = df.encode()
    assert encoded == b"\x00\x06" + INVERTER + b"\x15\x00\xff\x00"
    assert len(encoded) == 16


def test_data_frame_write_single_round_trip():
    frame = make_write_single()
    decoded = Frame.decode(frame.encode()).data_frame()
    assert decoded.device_function == DeviceFunction.WRITE_SINGLE
    assert decoded.register == 21
    assert decoded.value == struct.pack("<H", 0x00FF)
    assert decoded.has_length_byte is False


def test_data_frame_read_response_has_length_byte():
    # protocol 2 + READ_INPUT => value preceded by a length byte.
    values = struct.pack("<HH", 10, 20)
    df = DataFrame(
        action=1,
        device_function=DeviceFunction.READ_INPUT,
        inverter_serial=INVERTER,
        register=0,
        value=values,
        has_length_byte=True,
    )
    encoded = df.encode()
    assert encoded[14] == len(values)
    decoded = DataFrame.decode(encoded, protocol=2)
    assert decoded == df
    assert decoded.value == values


# --- Error handling ----------------------------------------------------------


def test_bad_prefix_raises():
    packet = bytearray(make_write_single().encode())
    packet[0] = 0x00
    with pytest.raises(PrefixError):
        Frame.decode(bytes(packet))


def test_corrupted_crc_raises():
    packet = bytearray(make_write_single().encode())
    packet[-1] ^= 0xFF
    with pytest.raises(CrcError):
        Frame.decode(bytes(packet))


def test_truncated_packet_raises():
    packet = make_write_single().encode()[:10]
    with pytest.raises(TruncatedFrameError):
        Frame.decode(packet)


def test_truncated_by_frame_length_raises():
    packet = make_write_single().encode()[:-5]
    with pytest.raises(TruncatedFrameError):
        Frame.decode(packet)


def test_wrong_serial_length_rejected():
    with pytest.raises(ValueError, match="dongle_serial"):
        Frame(
            tcp_function=TcpFunction.TRANSLATED_DATA,
            dongle_serial=b"short",
            data=b"",
        )


# --- Real captures (two-tier ground-truth) -----------------------------------

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.bin"))


@pytest.mark.skipif(not FIXTURES, reason="no captured packets in tests/fixtures")
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_captured_frames_round_trip(path: Path):
    raw = path.read_bytes()
    frame = Frame.decode(raw)
    assert frame.encode() == raw
