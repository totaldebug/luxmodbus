"""Passive register discovery for LuxPower inverters.

The inverter exposes a contiguous register space; the known map in
:mod:`luxmodbus.registers` only covers the addresses we understand. Discovery
watches normal traffic and records every register that appears but is *not* in
the map, together with a rolling history of how its value changes — the raw
material for working out what an unknown register means.

This is the **passive** half of discovery and is read-only: it only inspects
values that were going to be decoded anyway. There is no I/O here and no Home
Assistant dependency; the active register *sweep* (sending extra reads) belongs
to the transport/integration layer, which feeds its responses back through
:meth:`DiscoveryStore.observe_many` exactly like normal traffic.

State is JSON-serialisable (:meth:`DiscoveryStore.to_dict` /
:meth:`DiscoveryStore.load`) so the integration can persist it across restarts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from luxmodbus.registers import RegisterBank, mapped_hold_addresses, mapped_input_addresses

__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DiscoveryStore",
    "UnknownRegister",
]

DEFAULT_HISTORY_LIMIT = 32


@dataclass
class UnknownRegister:
    """A register seen in traffic but absent from the known map.

    ``history`` is a bounded list of ``(timestamp, value)`` pairs recorded each
    time the value *changes*, newest last; ``times_seen`` counts every sighting
    including unchanged ones.
    """

    bank: RegisterBank
    address: int
    first_seen: float
    last_seen: float
    last_value: int
    times_seen: int = 1
    history: list[tuple[float, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of this record."""
        return {
            "bank": self.bank.value,
            "address": self.address,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_value": self.last_value,
            "times_seen": self.times_seen,
            "history": [[t, v] for t, v in self.history],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UnknownRegister:
        """Rebuild a record from :meth:`to_dict` output."""
        return cls(
            bank=RegisterBank(data["bank"]),
            address=int(data["address"]),
            first_seen=float(data["first_seen"]),
            last_seen=float(data["last_seen"]),
            last_value=int(data["last_value"]),
            times_seen=int(data["times_seen"]),
            history=[(float(t), int(v)) for t, v in data["history"]],
        )


class DiscoveryStore:
    """Diffs observed registers against the known map and records the unknown.

    ``known`` maps each :class:`~luxmodbus.registers.RegisterBank` to the set of
    addresses that are already understood. Use :meth:`from_registers` to wire in
    the real luxmodbus map, or pass an explicit mapping (handy for tests).
    """

    def __init__(
        self,
        known: Mapping[RegisterBank, Iterable[int]],
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._known: dict[RegisterBank, frozenset[int]] = {
            bank: frozenset(addresses) for bank, addresses in known.items()
        }
        self._history_limit = history_limit
        self._clock = clock
        self._records: dict[tuple[RegisterBank, int], UnknownRegister] = {}

    @classmethod
    def from_registers(
        cls,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        clock: Callable[[], float] = time.time,
    ) -> DiscoveryStore:
        """Build a store using the addresses known to :mod:`luxmodbus.registers`."""
        known = {
            RegisterBank.INPUT: mapped_input_addresses(),
            RegisterBank.HOLD: mapped_hold_addresses(),
        }
        return cls(known, history_limit=history_limit, clock=clock)

    def is_known(self, bank: RegisterBank, address: int) -> bool:
        """Whether ``address`` in ``bank`` is already in the known map."""
        return address in self._known.get(bank, frozenset())

    def observe(
        self,
        bank: RegisterBank,
        address: int,
        value: int,
        *,
        timestamp: float | None = None,
    ) -> UnknownRegister | None:
        """Record one observed register.

        Returns the :class:`UnknownRegister` record if the address is unmapped
        (creating or updating it), or ``None`` if the address is known.
        """
        if self.is_known(bank, address):
            return None
        ts = self._clock() if timestamp is None else timestamp
        key = (bank, address)
        record = self._records.get(key)
        if record is None:
            record = UnknownRegister(
                bank=bank,
                address=address,
                first_seen=ts,
                last_seen=ts,
                last_value=value,
                times_seen=1,
                history=[(ts, value)],
            )
            self._records[key] = record
            return record
        record.times_seen += 1
        record.last_seen = ts
        if value != record.last_value:
            record.history.append((ts, value))
            if len(record.history) > self._history_limit:
                del record.history[0]
            record.last_value = value
        return record

    def observe_many(
        self,
        bank: RegisterBank,
        values: Mapping[int, int],
        *,
        timestamp: float | None = None,
    ) -> list[UnknownRegister]:
        """Observe an ``{address: value}`` map for one bank.

        Returns the records that were seen for the very first time in this call
        (i.e. the new registers worth raising an event for).
        """
        ts = self._clock() if timestamp is None else timestamp
        new: list[UnknownRegister] = []
        for address, value in values.items():
            existed = (bank, address) in self._records
            record = self.observe(bank, address, value, timestamp=ts)
            if record is not None and not existed:
                new.append(record)
        return new

    @property
    def unknown(self) -> dict[tuple[RegisterBank, int], UnknownRegister]:
        """All unknown registers seen so far, keyed by ``(bank, address)``."""
        return dict(self._records)

    def count(self) -> int:
        """Number of distinct unknown registers recorded."""
        return len(self._records)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of all recorded registers."""
        return {"records": [record.to_dict() for record in self._records.values()]}

    def load(self, data: Mapping[str, Any]) -> None:
        """Restore records from a :meth:`to_dict` snapshot, replacing current state."""
        self._records = {}
        for item in data.get("records", []):
            record = UnknownRegister.from_dict(item)
            self._records[(record.bank, record.address)] = record
