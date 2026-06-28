#!/usr/bin/env python3
"""Decode a LuxPower capture and report which registers are known vs unknown.

The companion to ``docs/capturing-packets.md``: point it at captured dongle
traffic and it decodes every frame, looks each register up in the map, and
prints what is known (with its decoded value), what is unknown, and which
registers the app wrote. Pass ``--state`` to diff against the previous run so
you can change one setting in the app and see exactly which register moved.

Inputs (pick one):
  --hex FILE   a tshark ``-e data.data`` hex dump (concatenated TCP payload)
  --bin GLOB   one or more ``*.bin`` frame files
  --pcap FILE  a pcap (shells out to tshark to export the payload)

Examples:
  tshark -r lux.pcap -Y 'tcp.len>0' -T fields -e data.data | tr -d '\\n:' > lux.hex
  python scripts/analyze_capture.py --hex lux.hex --state /tmp/lux_state.json
"""

from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

from luxmodbus import (
    FLAG_REGISTERS,
    HOLD_REGISTERS,
    INPUT_REGISTERS,
    SELECT_REGISTERS,
    TIME_REGISTERS,
    DeviceFunction,
    FlagRegister,
    Frame,
    ProtocolError,
    RegisterBank,
    RegisterDef,
    SelectRegister,
    TimeRegister,
    decode_flags,
    decode_select,
    decode_time,
    decode_value,
    extract_frames,
    mapped_hold_addresses,
    mapped_input_addresses,
)

MapEntry = RegisterDef | FlagRegister | TimeRegister | SelectRegister

_READ = {DeviceFunction.READ_INPUT, DeviceFunction.READ_HOLD}
_BANK_FOR = {DeviceFunction.READ_INPUT: RegisterBank.INPUT, DeviceFunction.READ_HOLD: RegisterBank.HOLD}


def load_stream(args: argparse.Namespace) -> bytes:
    """Return the raw TCP byte stream from whichever input option was given."""
    if args.hex:
        return bytes.fromhex("".join(Path(args.hex).read_text().split()).replace(":", ""))
    if args.pcap:
        result = subprocess.run(
            ["tshark", "-r", args.pcap, "-Y", "tcp.len>0", "-T", "fields", "-e", "data.data"],
            capture_output=True,
            text=True,
            check=True,
        )
        return bytes.fromhex("".join(result.stdout.split()).replace(":", ""))
    return b"".join(Path(path).read_bytes() for path in sorted(glob.glob(args.bin)))


def lookup(bank: RegisterBank, address: int) -> list[MapEntry]:
    """Return every map entry covering ``address`` (a register can mean more than one thing).

    Register 110, for example, is both a flag register and the on-grid-working-mode select.
    """
    if bank is RegisterBank.INPUT:
        return [defn for defn in INPUT_REGISTERS if address in defn.addresses()]
    entries: list[MapEntry] = [defn for defn in HOLD_REGISTERS if address in defn.addresses()]
    entries += [entry for entry in FLAG_REGISTERS if entry.address == address]
    entries += [entry for entry in TIME_REGISTERS if entry.address == address]
    entries += [entry for entry in SELECT_REGISTERS if entry.address == address]
    return entries


def decoded(entry: MapEntry, raw: int, raw_map: dict[int, int]) -> float | int | str | None:
    """Decode a known register's value according to its kind."""
    if isinstance(entry, RegisterDef):
        return decode_value(entry, raw_map)
    if isinstance(entry, TimeRegister):
        hour, minute = decode_time(raw)
        return f"{hour:02d}:{minute:02d}"
    if isinstance(entry, SelectRegister):
        return decode_select(raw, entry)
    on = [flag.key for flag, is_set in zip(entry.flags, decode_flags(raw, entry).values(), strict=False) if is_set]
    return "{" + ", ".join(on) + "}" if on else "(no bits set)"


def describe(bank: RegisterBank, address: int, raw_map: dict[int, int]) -> str:
    """One-line description: each known meaning + decoded value, or UNKNOWN + raw."""
    raw = raw_map[address]
    entries = lookup(bank, address)
    if not entries:
        return f"  [{bank.value:5}] {address:<5} UNKNOWN  raw={raw} (0x{raw:04x})"
    meanings = "  |  ".join(f"{entry.key} = {decoded(entry, raw, raw_map)} ({entry.name})" for entry in entries)
    return f"  [{bank.value:5}] {address:<5} {meanings}"


def collect(frames: list[bytes]) -> tuple[dict[RegisterBank, dict[int, int]], list[tuple[int, int]], int]:
    """Decode frames into (raw values per bank, list of writes, undecodable count)."""
    raw: dict[RegisterBank, dict[int, int]] = {RegisterBank.INPUT: {}, RegisterBank.HOLD: {}}
    writes: list[tuple[int, int]] = []
    bad = 0
    for frame_bytes in frames:
        try:
            frame = Frame.decode(frame_bytes)
        except ProtocolError:
            bad += 1
            continue
        data = frame.data
        if len(data) < 16:
            continue
        function = data[1]
        register = int.from_bytes(data[12:14], "little")
        if function in _READ:
            if len(data) <= 16:
                continue  # a read request carries only a count, no values
            length = data[14]
            values = data[15 : 15 + length]
            bank = _BANK_FOR[DeviceFunction(function)]
            for index in range(length // 2):
                raw[bank][register + index] = int.from_bytes(values[index * 2 : index * 2 + 2], "little")
        elif function == DeviceFunction.WRITE_SINGLE:
            value = int.from_bytes(data[14:16], "little")
            raw[RegisterBank.HOLD][register] = value
            writes.append((register, value))
    return raw, writes, bad


def report_diff(state_path: str, raw: dict[RegisterBank, dict[int, int]]) -> None:
    """Print NEW/CHANGED registers vs the saved snapshot, then update it."""
    path = Path(state_path)
    previous: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
    current = {f"{bank.value}:{address}": value for bank, values in raw.items() for address, value in values.items()}
    changed = [key for key, value in current.items() if key in previous and previous[key] != value]
    new = [key for key in current if key not in previous]
    if changed:
        print("\nCHANGED since last run: " + ", ".join(f"{key} {previous[key]}->{current[key]}" for key in changed))
    if new:
        print("NEW since last run: " + ", ".join(new))
    path.write_text(json.dumps(current))


def main() -> int:
    """Parse arguments, decode the capture and print the report."""
    parser = argparse.ArgumentParser(description="Analyze a LuxPower capture for unknown registers.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--hex", help="tshark -e data.data hex dump")
    source.add_argument("--bin", help="glob of *.bin frame files")
    source.add_argument("--pcap", help="pcap file (requires tshark)")
    parser.add_argument("--state", help="JSON snapshot to diff against and update")
    args = parser.parse_args()

    frames, _ = extract_frames(load_stream(args))
    raw, writes, bad = collect(frames)
    mapped = {RegisterBank.INPUT: mapped_input_addresses(), RegisterBank.HOLD: mapped_hold_addresses()}

    print(f"{len(frames)} frames ({bad} undecodable). Registers seen:")
    for bank in (RegisterBank.INPUT, RegisterBank.HOLD):
        for address in sorted(raw[bank]):
            print(describe(bank, address, raw[bank]))

    unknown = [(bank, address) for bank in raw for address in raw[bank] if address not in mapped[bank]]
    print(f"\n{len(unknown)} UNKNOWN register(s):")
    for bank, address in sorted(unknown, key=lambda item: (item[0].value, item[1])):
        print(describe(bank, address, raw[bank]))

    if writes:
        print(f"\n{len(writes)} WRITE(s) seen (app changed a setting):")
        for register, value in writes:
            entries = lookup(RegisterBank.HOLD, register)
            tag = ", ".join(f"{entry.key} ({entry.name})" for entry in entries) if entries else "UNKNOWN"
            print(f"  hold {register:<5} = {value} (0x{value:04x})   {tag}")

    if args.state:
        report_diff(args.state, raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
