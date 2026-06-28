"""Framing for the LuxPower inverter Modbus protocol.

This module knows how bytes are laid out on the wire and nothing about what any
register *means* — that lives in ``registers.py``. It has no I/O and no Home
Assistant dependency, so it can be exercised entirely offline against captured
or synthesised packet bytes.

Wire format (little-endian throughout)::

    prefix(2)=A1 1A | protocol(u16) | frame_length(u16) | reserved(1)=01 |
    tcp_function(1) | dongle_serial(10) | data_length(u16) | data_frame(N) | crc(u16)

with ``frame_length = total_len - 6`` and ``data_length = len(data_frame) + 2``
(the +2 is the trailing CRC). The CRC is the standard Modbus CRC-16 over the
data frame, appended low byte first.

The inner data frame::

    action(1) | device_function(1) | inverter_serial(10) | register(u16) | value

``value`` is preceded by a one-byte length when ``protocol`` is 2 or 5 and the
device function is not WRITE_SINGLE (this is the shape of read responses);
otherwise it is two raw bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "HEADER_LEN",
    "PREFIX",
    "RESERVED_BYTE",
    "SERIAL_LEN",
    "CrcError",
    "DataFrame",
    "DeviceFunction",
    "Frame",
    "PrefixError",
    "ProtocolError",
    "TcpFunction",
    "TruncatedFrameError",
    "crc16",
    "extract_frames",
]

# --- Constants (protocol facts) ---------------------------------------------

PREFIX = b"\xa1\x1a"
RESERVED_BYTE = 0x01
SERIAL_LEN = 10
# prefix(2) + protocol(2) + frame_length(2) + reserved(1) + tcp_function(1)
# + dongle_serial(10) + data_length(2)
HEADER_LEN = 20
_DEVICE_ERROR_FLAG = 0x80


class TcpFunction(IntEnum):
    """Outer envelope function code (byte 7)."""

    HEARTBEAT = 193
    TRANSLATED_DATA = 194
    READ_PARAM = 195
    WRITE_PARAM = 196


class DeviceFunction(IntEnum):
    """Inner Modbus function code (data frame byte 1)."""

    READ_HOLD = 3
    READ_INPUT = 4
    WRITE_SINGLE = 6
    WRITE_MULTI = 16

    @property
    def is_error(self) -> bool:
        """Whether this function code has the Modbus error flag (0x80) set."""
        return bool(self.value & _DEVICE_ERROR_FLAG)


# --- Errors ------------------------------------------------------------------


class ProtocolError(Exception):
    """Base class for framing errors."""


class PrefixError(ProtocolError):
    """Frame did not begin with the expected prefix."""


class TruncatedFrameError(ProtocolError):
    """Buffer is shorter than the frame it claims to contain."""


class CrcError(ProtocolError):
    """The trailing CRC did not match the computed CRC over the data frame."""


# --- CRC-16 (Modbus) ---------------------------------------------------------


def crc16(data: bytes) -> int:
    """Return the Modbus CRC-16 of ``data``.

    Textbook reflected algorithm: polynomial 0xA001, initial value 0xFFFF.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _u16(buf: bytes, offset: int) -> int:
    """Read a little-endian unsigned 16-bit int from ``buf`` at ``offset``."""
    return struct.unpack_from("<H", buf, offset)[0]


# --- Inner data frame --------------------------------------------------------


def _has_length_byte(protocol: int, device_function: int) -> bool:
    """Whether the value field is preceded by a length byte on the wire.

    True for read responses (protocol 2/5, any function except a single write).
    """
    return protocol in (2, 5) and device_function != DeviceFunction.WRITE_SINGLE


@dataclass(frozen=True)
class DataFrame:
    """The inner Modbus data frame, meaning-agnostic.

    ``value`` holds the raw register payload bytes. ``has_length_byte`` records
    whether those bytes were (or should be) prefixed by a one-byte length on the
    wire, so that decode/encode round-trips exactly.
    """

    action: int
    device_function: int
    inverter_serial: bytes
    register: int
    value: bytes
    has_length_byte: bool = False

    def __post_init__(self) -> None:
        """Validate the inverter serial length."""
        if len(self.inverter_serial) != SERIAL_LEN:
            raise ValueError(f"inverter_serial must be {SERIAL_LEN} bytes, got {len(self.inverter_serial)}")

    @classmethod
    def decode(cls, data: bytes, protocol: int) -> DataFrame:
        """Decode a received data frame. ``protocol`` comes from the envelope."""
        if len(data) < 14:
            raise TruncatedFrameError(f"data frame too short: {len(data)} bytes")
        action = data[0]
        device_function = data[1]
        inverter_serial = data[2:12]
        register = _u16(data, 12)
        has_length = _has_length_byte(protocol, device_function)
        if has_length:
            length = data[14]
            value = data[15 : 15 + length]
            if len(value) != length:
                raise TruncatedFrameError(f"value length byte says {length}, only {len(value)} present")
        else:
            value = data[14:16]
        return cls(
            action=action,
            device_function=device_function,
            inverter_serial=inverter_serial,
            register=register,
            value=value,
            has_length_byte=has_length,
        )

    def encode(self) -> bytes:
        """Serialise this data frame (without the trailing CRC)."""
        head = (
            struct.pack("<BB", self.action, self.device_function)
            + self.inverter_serial
            + struct.pack("<H", self.register)
        )
        if self.has_length_byte:
            return head + struct.pack("<B", len(self.value)) + self.value
        return head + self.value


# --- Outer envelope ----------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """A complete LuxPower TCP frame.

    ``data`` is the opaque inner data frame (without its CRC). The CRC is computed
    on encode and verified on decode, so callers never handle it directly.
    """

    tcp_function: int
    dongle_serial: bytes
    data: bytes
    protocol: int = 2
    reserved: int = RESERVED_BYTE

    def __post_init__(self) -> None:
        """Validate the dongle serial length."""
        if len(self.dongle_serial) != SERIAL_LEN:
            raise ValueError(f"dongle_serial must be {SERIAL_LEN} bytes, got {len(self.dongle_serial)}")

    @classmethod
    def decode(cls, packet: bytes) -> Frame:
        """Decode a complete frame, validating the prefix and CRC."""
        if len(packet) < HEADER_LEN + 2:
            raise TruncatedFrameError(f"packet too short: {len(packet)} bytes")
        if packet[0:2] != PREFIX:
            raise PrefixError(f"bad prefix: {packet[0:2]!r}")
        protocol = _u16(packet, 2)
        frame_length = _u16(packet, 4)
        total = frame_length + 6
        if len(packet) < total:
            raise TruncatedFrameError(f"frame_length implies {total} bytes, only {len(packet)} present")
        reserved = packet[6]
        tcp_function = packet[7]
        dongle_serial = packet[8:18]
        data_length = _u16(packet, 18)
        data = packet[HEADER_LEN : total - 2]
        crc = _u16(packet, total - 2)
        if data_length != len(data) + 2:
            raise TruncatedFrameError(f"data_length {data_length} != len(data)+2 ({len(data) + 2})")
        expected = crc16(data)
        if crc != expected:
            raise CrcError(f"crc {crc:#06x} != computed {expected:#06x}")
        return cls(
            tcp_function=tcp_function,
            dongle_serial=dongle_serial,
            data=data,
            protocol=protocol,
            reserved=reserved,
        )

    def encode(self) -> bytes:
        """Serialise the frame, computing length fields and the CRC."""
        data_length = len(self.data) + 2
        frame_length = HEADER_LEN - 6 + len(self.data) + 2
        header = (
            PREFIX
            + struct.pack("<HH", self.protocol, frame_length)
            + struct.pack("<BB", self.reserved, self.tcp_function)
            + self.dongle_serial
            + struct.pack("<H", data_length)
        )
        return header + self.data + struct.pack("<H", crc16(self.data))

    def data_frame(self) -> DataFrame:
        """Decode the inner data frame using this envelope's protocol."""
        return DataFrame.decode(self.data, self.protocol)


def extract_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a TCP byte stream into complete frames.

    Returns ``(frames, remainder)`` where ``frames`` are complete frame byte
    strings (each starting with :data:`PREFIX`) and ``remainder`` is the
    leftover bytes to prepend to the next chunk. Bytes before the first prefix
    are discarded (resync); a trailing partial frame — or a lone leading prefix
    byte — is held back in ``remainder``.
    """
    frames: list[bytes] = []
    offset = 0
    length = len(buffer)
    while True:
        start = buffer.find(PREFIX, offset)
        if start == -1:
            # No full prefix; keep a trailing half-prefix byte for next time.
            if length and buffer[-1] == PREFIX[0]:
                return frames, buffer[-1:]
            return frames, b""
        if start + 6 > length:
            return frames, buffer[start:]  # not enough to read frame_length yet
        total = _u16(buffer, start + 4) + 6
        if start + total > length:
            return frames, buffer[start:]  # incomplete frame
        frames.append(buffer[start : start + total])
        offset = start + total
