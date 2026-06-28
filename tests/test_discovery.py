"""Tests for luxmodbus.discovery — the passive diff-and-log engine."""

from __future__ import annotations

import itertools

from luxmodbus.discovery import DiscoveryStore, UnknownRegister
from luxmodbus.registers import RegisterBank, mapped_input_addresses


def make_store(history_limit: int = 32) -> DiscoveryStore:
    """A store that knows only input addresses {0, 1, 2} and hold address {21}."""
    clock = itertools.count(1000).__next__  # deterministic, monotonic timestamps
    return DiscoveryStore(
        {RegisterBank.INPUT: {0, 1, 2}, RegisterBank.HOLD: {21}},
        history_limit=history_limit,
        clock=lambda: float(clock()),
    )


# --- Basic diffing -----------------------------------------------------------


def test_known_register_is_ignored():
    store = make_store()
    assert store.observe(RegisterBank.INPUT, 1, 1234) is None
    assert store.count() == 0


def test_unknown_register_is_recorded():
    store = make_store()
    record = store.observe(RegisterBank.INPUT, 999, 42)
    assert record is not None
    assert record.address == 999
    assert record.bank is RegisterBank.INPUT
    assert record.last_value == 42
    assert record.times_seen == 1
    assert store.count() == 1


def test_same_address_different_bank_is_distinct():
    store = make_store()
    # input 21 is unknown here (known input set is {0,1,2}); hold 21 is known.
    assert store.observe(RegisterBank.INPUT, 21, 5) is not None
    assert store.observe(RegisterBank.HOLD, 21, 5) is None
    assert store.count() == 1


# --- History tracking --------------------------------------------------------


def test_history_records_only_changes():
    store = make_store()
    store.observe(RegisterBank.INPUT, 999, 10)
    store.observe(RegisterBank.INPUT, 999, 10)  # unchanged
    store.observe(RegisterBank.INPUT, 999, 11)  # changed
    record = store.unknown[(RegisterBank.INPUT, 999)]
    assert record.times_seen == 3
    assert [v for _, v in record.history] == [10, 11]
    assert record.last_value == 11


def test_history_is_bounded():
    store = make_store(history_limit=3)
    for value in range(10):
        store.observe(RegisterBank.INPUT, 999, value)
    record = store.unknown[(RegisterBank.INPUT, 999)]
    assert len(record.history) == 3
    assert [v for _, v in record.history] == [7, 8, 9]  # newest kept


# --- observe_many ------------------------------------------------------------


def test_observe_many_returns_only_new():
    store = make_store()
    first = store.observe_many(RegisterBank.INPUT, {0: 1, 1: 2, 500: 99, 501: 7})
    assert {r.address for r in first} == {500, 501}
    # Second pass: 500/501 already known to the store, 502 is new.
    second = store.observe_many(RegisterBank.INPUT, {500: 99, 502: 3})
    assert {r.address for r in second} == {502}
    assert store.count() == 3


# --- Persistence -------------------------------------------------------------


def test_round_trip_to_dict_and_load():
    store = make_store()
    store.observe(RegisterBank.INPUT, 999, 10)
    store.observe(RegisterBank.INPUT, 999, 11)
    store.observe(RegisterBank.HOLD, 300, 7)
    snapshot = store.to_dict()

    restored = make_store()
    restored.load(snapshot)
    assert restored.count() == 2
    rec = restored.unknown[(RegisterBank.INPUT, 999)]
    assert rec.last_value == 11
    assert rec.history == [(1000.0, 10), (1001.0, 11)]
    assert rec.bank is RegisterBank.INPUT


def test_unknown_register_dict_round_trip():
    rec = UnknownRegister(
        bank=RegisterBank.HOLD,
        address=300,
        first_seen=1.0,
        last_seen=2.0,
        last_value=9,
        times_seen=4,
        history=[(1.0, 8), (2.0, 9)],
    )
    assert UnknownRegister.from_dict(rec.to_dict()) == rec


# --- from_registers ----------------------------------------------------------


def test_from_registers_knows_the_real_map():
    store = DiscoveryStore.from_registers()
    known_addr = next(iter(mapped_input_addresses()))
    assert store.is_known(RegisterBank.INPUT, known_addr)
    # 9000 is well outside the documented register space.
    assert store.observe(RegisterBank.INPUT, 9000, 1) is not None
