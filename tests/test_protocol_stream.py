"""Tests for luxmodbus.protocol.extract_frames (TCP stream reassembly)."""

from __future__ import annotations

import struct

from luxmodbus.protocol import DeviceFunction, Frame, TcpFunction, extract_frames

INVERTER = b"1234567890"
DONGLE = b"BA12345678"


def a_frame(register: int) -> bytes:
    """Build a small encoded frame for use as stream test data."""
    data = (
        struct.pack("<BB", 0, DeviceFunction.WRITE_SINGLE) + INVERTER + struct.pack("<HH", register, 0)
    )
    return Frame(tcp_function=TcpFunction.TRANSLATED_DATA, dongle_serial=DONGLE, data=data).encode()


def test_single_complete_frame():
    frame = a_frame(21)
    frames, remainder = extract_frames(frame)
    assert frames == [frame]
    assert remainder == b""


def test_two_frames_in_one_buffer():
    f1, f2 = a_frame(1), a_frame(2)
    frames, remainder = extract_frames(f1 + f2)
    assert frames == [f1, f2]
    assert remainder == b""


def test_partial_frame_is_held_back():
    frame = a_frame(7)
    frames, remainder = extract_frames(frame[:-3])
    assert frames == []
    assert remainder == frame[:-3]
    # Feeding the rest completes it.
    frames, remainder = extract_frames(remainder + frame[-3:])
    assert frames == [frame]
    assert remainder == b""


def test_leading_garbage_is_resynced():
    frame = a_frame(9)
    frames, remainder = extract_frames(b"\x00\xff\x99" + frame)
    assert frames == [frame]
    assert remainder == b""


def test_lone_prefix_byte_is_retained():
    frames, remainder = extract_frames(b"\x00\xa1")
    assert frames == []
    assert remainder == b"\xa1"


def test_header_split_across_chunks():
    frame = a_frame(3)
    # Only the first 5 bytes: prefix + protocol + one length byte (can't read length).
    frames, remainder = extract_frames(frame[:5])
    assert frames == []
    assert remainder == frame[:5]
    frames, remainder = extract_frames(remainder + frame[5:])
    assert frames == [frame]
