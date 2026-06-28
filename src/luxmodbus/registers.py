"""Declarative register map for LuxPower inverters — the single source of truth.

This module is *data*: each register is described once (address, type, scale,
unit, a neutral measurement kind) and that description drives both decoding and,
in the Home Assistant integration, entity generation. There are no Home
Assistant imports here — ``measurement`` is a hardware-neutral enum the
integration maps onto HA device/state classes.

All facts (addresses, scales, units, bit assignments) are taken from the
official Lux Power "Modbus RTU Protocol" specification and written fresh as a
table; no code is copied from other implementations.

Two banks:

* **input** registers (Modbus fn 0x04) — read-only telemetry → sensors.
* **hold** registers (fn 0x03/0x06/0x10) — settings → numbers / switches /
  selects.

Scales follow the spec's unit column: ``0.1V`` means raw * 0.1 volts,
``0.01Hz`` means raw * 0.01 Hz, ``0.1kWh`` means raw * 0.1 kWh, and so on.
32-bit quantities are stored little-word-first across two consecutive registers
(the spec's ``*_all L`` / ``*_all H`` pairs).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "FLAG_REGISTERS",
    "HOLD_REGISTERS",
    "INPUT_REGISTERS",
    "SELECT_REGISTERS",
    "TIME_REGISTERS",
    "ByteSelect",
    "FlagDef",
    "FlagRegister",
    "Measurement",
    "RegisterBank",
    "RegisterDef",
    "SelectRegister",
    "TimeRegister",
    "ValueType",
    "decode_flags",
    "decode_holds",
    "decode_inputs",
    "decode_select",
    "decode_time",
    "decode_value",
    "encode_time",
    "encode_value",
    "find_hold",
    "find_input",
    "mapped_hold_addresses",
    "mapped_input_addresses",
    "set_flag",
    "set_select",
]


class RegisterBank(Enum):
    """Which Modbus register bank a definition belongs to."""

    INPUT = "input"
    HOLD = "hold"


class ValueType(Enum):
    """How the raw register word(s) are interpreted."""

    U16 = "u16"
    S16 = "s16"
    U32 = "u32"  # little-word-first across address, address+1
    S32 = "s32"
    ASCII = "ascii"  # text spanning RegisterDef.length registers, two chars each

    @property
    def words(self) -> int:
        """Number of 16-bit registers this value spans (ASCII is variable; see RegisterDef.length)."""
        return 2 if self in (ValueType.U32, ValueType.S32) else 1

    @property
    def signed(self) -> bool:
        """Whether the value is two's-complement signed."""
        return self in (ValueType.S16, ValueType.S32)


class ByteSelect(Enum):
    """Select one byte of a 16-bit word (e.g. register 5 packs SOC and SOH)."""

    LOW = "low"
    HIGH = "high"


class Measurement(Enum):
    """Hardware-neutral measurement kind. The integration maps this to HA."""

    NONE = "none"
    POWER = "power"
    APPARENT_POWER = "apparent_power"
    REACTIVE_POWER = "reactive_power"
    ENERGY = "energy"
    VOLTAGE = "voltage"
    CURRENT = "current"
    FREQUENCY = "frequency"
    TEMPERATURE = "temperature"
    PERCENT = "percent"
    POWER_FACTOR = "power_factor"
    DURATION = "duration"
    COUNT = "count"
    ENUM = "enum"


# --- Value transforms --------------------------------------------------------


def _power_factor(raw: int) -> float:
    """Fold the packed power-factor encoding to a signed ratio in [-1, 1].

    Spec: x in (0, 1000] -> x / 1000; x in (1000, 2000) -> (1000 - x) / 1000.
    """
    if raw > 1000:
        return round((1000 - raw) / 1000, 3)
    return round(raw / 1000, 3)


TRANSFORMS = {"power_factor": _power_factor}


# --- Register definition -----------------------------------------------------


@dataclass(frozen=True)
class RegisterDef:
    """One register's meaning. ``key`` is the stable identifier for entities."""

    key: str
    address: int
    name: str
    bank: RegisterBank
    measurement: Measurement = Measurement.NONE
    type: ValueType = ValueType.U16
    scale: float = 1.0
    unit: str | None = None
    byte: ByteSelect | None = None
    transform: str | None = None
    writable: bool = False
    value_min: float | None = None
    value_max: float | None = None
    enabled_default: bool = True
    length: int = 0  # number of registers for ASCII strings; ignored otherwise

    @property
    def word_count(self) -> int:
        """Number of registers this definition spans (``length`` for ASCII, else by type)."""
        return self.length if self.type is ValueType.ASCII else self.type.words

    def addresses(self) -> tuple[int, ...]:
        """Every raw register address this definition consumes."""
        return tuple(self.address + i for i in range(self.word_count))


@dataclass(frozen=True)
class FlagDef:
    """A single bit within a bit-packed register."""

    bit: int
    key: str
    name: str
    enabled_default: bool = True


@dataclass(frozen=True)
class FlagRegister:
    """A bit-packed hold register whose bits are individual on/off flags."""

    key: str
    address: int
    name: str
    flags: tuple[FlagDef, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TimeRegister:
    """A hold register encoding a time of day as HH:MM.

    The spec stores each schedule edge's ``*Hour`` and ``*Minute`` fields in one
    register: the low byte is the hour (0-23), the high byte the minute (0-59).
    """

    key: str
    address: int
    name: str
    enabled_default: bool = True


@dataclass(frozen=True)
class SelectRegister:
    """A hold register field whose value picks one of several named options.

    ``options[i]`` is the label for raw field value ``i``. The field occupies the
    bits set in ``mask`` (a whole-register enum uses ``mask=0xFFFF``); the value is
    shifted down by ``mask``'s lowest set bit before indexing.
    """

    key: str
    address: int
    name: str
    options: tuple[str, ...]
    mask: int = 0xFFFF
    enabled_default: bool = True


# --- Decoding ----------------------------------------------------------------


def _raw_int(defn: RegisterDef, raw: Mapping[int, int]) -> int | None:
    """Assemble the raw integer for ``defn`` from word values, or None if absent."""
    if defn.type.words == 2:
        lo = raw.get(defn.address)
        hi = raw.get(defn.address + 1)
        if lo is None or hi is None:
            return None
        combined = (lo & 0xFFFF) | ((hi & 0xFFFF) << 16)
        if defn.type.signed and combined >= 0x8000_0000:
            combined -= 0x1_0000_0000
        return combined

    word = raw.get(defn.address)
    if word is None:
        return None
    word &= 0xFFFF
    if defn.byte is ByteSelect.LOW:
        return word & 0xFF
    if defn.byte is ByteSelect.HIGH:
        return (word >> 8) & 0xFF
    if defn.type.signed and word >= 0x8000:
        word -= 0x1_0000
    return word


def _decode_ascii(defn: RegisterDef, raw: Mapping[int, int]) -> str | None:
    """Decode an ASCII string register (two chars per word, low byte first), or None if absent."""
    chars: list[str] = []
    for offset in range(defn.word_count):
        word = raw.get(defn.address + offset)
        if word is None:
            return None
        chars.append(chr(word & 0xFF))
        chars.append(chr((word >> 8) & 0xFF))
    text = "".join(chars).rstrip("\x00 ").strip()
    return text or None


def decode_value(defn: RegisterDef, raw: Mapping[int, int]) -> float | int | str | None:
    """Decode one register definition to its scaled value, or None if not present."""
    if defn.type is ValueType.ASCII:
        return _decode_ascii(defn, raw)
    base = _raw_int(defn, raw)
    if base is None:
        return None
    if defn.transform is not None:
        return TRANSFORMS[defn.transform](base)
    if defn.scale == 1.0:
        return base
    return round(base * defn.scale, 3)


def encode_value(defn: RegisterDef, value: float) -> int:
    """Encode a scaled value into a raw register word for writing.

    The inverse of :func:`decode_value` for plain numeric registers: it enforces
    the declared ``value_min`` / ``value_max`` bounds (in scaled units) and then
    divides out ``scale`` to recover the raw word. The result is a 0–65535 word
    (two's-complement for signed types) ready to hand to
    :meth:`luxmodbus.protocol.DataFrame.write_single`.

    Raises ``ValueError`` for register kinds with no plain numeric round-trip
    (ASCII, byte-select, ``transform``), for 32-bit registers, or for a value
    outside the declared range.
    """
    if defn.type is ValueType.ASCII or defn.byte is not None or defn.transform is not None:
        raise ValueError(f"{defn.key!r} has no plain numeric encoding")
    if defn.type.words != 1:
        raise ValueError(f"{defn.key!r} is a 32-bit register; multi-word writes are not supported")
    if defn.value_min is not None and value < defn.value_min:
        raise ValueError(f"{value} below minimum {defn.value_min} for {defn.key!r}")
    if defn.value_max is not None and value > defn.value_max:
        raise ValueError(f"{value} above maximum {defn.value_max} for {defn.key!r}")
    raw = round(value / defn.scale)
    if defn.type.signed:
        if not -0x8000 <= raw <= 0x7FFF:
            raise ValueError(f"encoded value {raw} out of s16 range for {defn.key!r}")
        return raw & 0xFFFF
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"encoded value {raw} out of u16 range for {defn.key!r}")
    return raw


def decode_inputs(raw: Mapping[int, int]) -> dict[str, float | int | str]:
    """Decode every input register present in ``raw`` to a ``{key: value}`` map."""
    return _decode_bank(INPUT_REGISTERS, raw)


def decode_holds(raw: Mapping[int, int]) -> dict[str, float | int | str]:
    """Decode every hold register present in ``raw`` to a ``{key: value}`` map."""
    return _decode_bank(HOLD_REGISTERS, raw)


def _decode_bank(registers: tuple[RegisterDef, ...], raw: Mapping[int, int]) -> dict[str, float | int | str]:
    """Decode each register in ``registers`` that has values present in ``raw``."""
    out: dict[str, float | int | str] = {}
    for defn in registers:
        value = decode_value(defn, raw)
        if value is not None:
            out[defn.key] = value
    return out


def decode_flags(register_value: int, flags: FlagRegister) -> dict[str, bool]:
    """Decode a bit-packed register value into ``{flag_key: bool}``."""
    return {f.key: bool(register_value & (1 << f.bit)) for f in flags.flags}


def set_flag(register_value: int, flag: FlagDef, on: bool) -> int:
    """Return ``register_value`` with ``flag``'s bit set or cleared."""
    mask = 1 << flag.bit
    return (register_value | mask) if on else (register_value & ~mask & 0xFFFF)


def decode_time(register_value: int) -> tuple[int, int]:
    """Split a packed time register into ``(hour, minute)`` (low byte / high byte)."""
    return register_value & 0xFF, (register_value >> 8) & 0xFF


def encode_time(hour: int, minute: int) -> int:
    """Pack ``hour`` (low byte) and ``minute`` (high byte) into one register word."""
    return ((minute & 0xFF) << 8) | (hour & 0xFF)


def _mask_shift(mask: int) -> int:
    """Bit offset of the lowest set bit in ``mask``."""
    return (mask & -mask).bit_length() - 1


def decode_select(register_value: int, select: SelectRegister) -> str | None:
    """Decode a select register's masked field to its option label, or None if out of range."""
    index = (register_value & select.mask) >> _mask_shift(select.mask)
    return select.options[index] if index < len(select.options) else None


def set_select(register_value: int, select: SelectRegister, option: str) -> int:
    """Return ``register_value`` with ``select``'s field set to ``option``'s index."""
    index = select.options.index(option)
    shifted = (index << _mask_shift(select.mask)) & select.mask
    return (register_value & ~select.mask & 0xFFFF) | shifted


# --- Input registers (telemetry → sensors) -----------------------------------
#
# Single-phase hybrid is the validated target. Three-phase (S/T phase), US
# split-phase (L1N/L2N), generator and extra-PV registers are mapped but
# default-disabled until confirmed on that hardware.

V = "V"
A = "A"
W = "W"
HZ = "Hz"
KWH = "kWh"
PCT = "%"
C = "°C"
VA = "VA"

INPUT_REGISTERS: tuple[RegisterDef, ...] = (
    RegisterDef("status", 0, "Status", RegisterBank.INPUT, Measurement.ENUM),
    RegisterDef(
        "pv1_voltage",
        1,
        "Solar Voltage Array 1",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
    ),
    RegisterDef(
        "pv2_voltage",
        2,
        "Solar Voltage Array 2",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
    ),
    RegisterDef(
        "pv3_voltage",
        3,
        "Solar Voltage Array 3",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "battery_voltage",
        4,
        "Battery Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
    ),
    RegisterDef(
        "soc",
        5,
        "Battery",
        RegisterBank.INPUT,
        Measurement.PERCENT,
        unit=PCT,
        byte=ByteSelect.LOW,
    ),
    RegisterDef(
        "soh",
        5,
        "State of Health",
        RegisterBank.INPUT,
        Measurement.PERCENT,
        unit=PCT,
        byte=ByteSelect.HIGH,
        enabled_default=False,
    ),
    RegisterDef("internal_fault", 6, "Internal Fault", RegisterBank.INPUT, enabled_default=False),
    RegisterDef(
        "pv1_power",
        7,
        "Solar Output Array 1",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "pv2_power",
        8,
        "Solar Output Array 2",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "pv3_power",
        9,
        "Solar Output Array 3",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "battery_charge_power",
        10,
        "Battery Charge",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "battery_discharge_power",
        11,
        "Battery Discharge",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "grid_voltage",
        12,
        "Grid Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
    ),
    RegisterDef(
        "grid_voltage_s",
        13,
        "Grid Voltage S",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "grid_voltage_t",
        14,
        "Grid Voltage T",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "grid_frequency",
        15,
        "Grid Frequency",
        RegisterBank.INPUT,
        Measurement.FREQUENCY,
        scale=0.01,
        unit=HZ,
    ),
    RegisterDef(
        "power_from_inverter",
        16,
        "Power From Inverter",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "power_to_inverter",
        17,
        "Power To Inverter",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "inverter_current",
        18,
        "Inverter Current",
        RegisterBank.INPUT,
        Measurement.CURRENT,
        scale=0.01,
        unit=A,
        enabled_default=False,
    ),
    RegisterDef(
        "power_factor",
        19,
        "Power Factor",
        RegisterBank.INPUT,
        Measurement.POWER_FACTOR,
        transform="power_factor",
    ),
    RegisterDef(
        "eps_voltage_l1",
        20,
        "EPS L1 Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
    ),
    RegisterDef(
        "eps_voltage_l2",
        21,
        "EPS L2 Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
    ),
    RegisterDef(
        "eps_voltage_t",
        22,
        "EPS T Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "eps_frequency",
        23,
        "EPS Frequency",
        RegisterBank.INPUT,
        Measurement.FREQUENCY,
        scale=0.01,
        unit=HZ,
        enabled_default=False,
    ),
    RegisterDef("power_to_eps", 24, "Power To EPS", RegisterBank.INPUT, Measurement.POWER, unit=W),
    RegisterDef(
        "eps_apparent_power",
        25,
        "EPS Apparent Power",
        RegisterBank.INPUT,
        Measurement.APPARENT_POWER,
        unit=VA,
        enabled_default=False,
    ),
    RegisterDef("power_to_grid", 26, "Power To Grid", RegisterBank.INPUT, Measurement.POWER, unit=W),
    RegisterDef(
        "power_from_grid",
        27,
        "Power From Grid",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "pv1_energy_today",
        28,
        "Solar Array 1 Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "pv2_energy_today",
        29,
        "Solar Array 2 Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "pv3_energy_today",
        30,
        "Solar Array 3 Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_energy_today",
        31,
        "Inverter to Home Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "ac_charge_energy_today",
        32,
        "To Inverter Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "battery_charge_energy_today",
        33,
        "Battery Charge Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "battery_discharge_energy_today",
        34,
        "Battery Discharge Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "eps_energy_today",
        35,
        "Power To EPS Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "energy_to_grid_today",
        36,
        "Power To Grid Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "energy_from_grid_today",
        37,
        "Power From Grid Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "bus1_voltage",
        38,
        "Bus 1 Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "bus2_voltage",
        39,
        "Bus 2 Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "pv1_energy_total",
        40,
        "Solar Array 1 Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "pv2_energy_total",
        42,
        "Solar Array 2 Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "pv3_energy_total",
        44,
        "Solar Array 3 Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_energy_total",
        46,
        "Inverter to Home Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "ac_charge_energy_total",
        48,
        "To Inverter Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "battery_charge_energy_total",
        50,
        "Battery Charge Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "battery_discharge_energy_total",
        52,
        "Battery Discharge Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "eps_energy_total",
        54,
        "Power To EPS Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "energy_to_grid_total",
        56,
        "Power To Grid Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "energy_from_grid_total",
        58,
        "Power From Grid Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "fault_code",
        60,
        "Fault Code",
        RegisterBank.INPUT,
        type=ValueType.U32,
        enabled_default=False,
    ),
    RegisterDef(
        "warning_code",
        62,
        "Warning Code",
        RegisterBank.INPUT,
        type=ValueType.U32,
        enabled_default=False,
    ),
    RegisterDef(
        "internal_temperature",
        64,
        "Internal Temperature",
        RegisterBank.INPUT,
        Measurement.TEMPERATURE,
        unit=C,
    ),
    RegisterDef(
        "radiator1_temperature",
        65,
        "Radiator 1 Temperature",
        RegisterBank.INPUT,
        Measurement.TEMPERATURE,
        unit=C,
    ),
    RegisterDef(
        "radiator2_temperature",
        66,
        "Radiator 2 Temperature",
        RegisterBank.INPUT,
        Measurement.TEMPERATURE,
        unit=C,
    ),
    RegisterDef(
        "battery_temperature",
        67,
        "Battery Temperature",
        RegisterBank.INPUT,
        Measurement.TEMPERATURE,
        unit=C,
    ),
    RegisterDef(
        "runtime",
        69,
        "Runtime",
        RegisterBank.INPUT,
        Measurement.DURATION,
        type=ValueType.U32,
        unit="s",
        enabled_default=False,
    ),
    RegisterDef(
        "bms_limit_charge",
        81,
        "BMS Limit Charge",
        RegisterBank.INPUT,
        Measurement.CURRENT,
        scale=0.01,
        unit=A,
    ),
    RegisterDef(
        "bms_limit_discharge",
        82,
        "BMS Limit Discharge",
        RegisterBank.INPUT,
        Measurement.CURRENT,
        scale=0.01,
        unit=A,
    ),
    RegisterDef(
        "bms_charge_voltage_ref",
        83,
        "BMS Charge Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "bms_discharge_cutoff_voltage",
        84,
        "BMS Discharge Cutoff Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef("battery_count", 96, "Battery Count", RegisterBank.INPUT, Measurement.COUNT),
    RegisterDef("battery_capacity", 97, "Battery Capacity", RegisterBank.INPUT, unit="Ah"),
    RegisterDef(
        "battery_current",
        98,
        "Battery Current",
        RegisterBank.INPUT,
        Measurement.CURRENT,
        type=ValueType.S16,
        scale=0.01,
        unit=A,
    ),
    RegisterDef(
        "max_cell_voltage",
        101,
        "Max Cell Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.001,
        unit=V,
    ),
    RegisterDef(
        "min_cell_voltage",
        102,
        "Min Cell Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.001,
        unit=V,
    ),
    RegisterDef(
        "max_cell_temperature",
        103,
        "Max Cell Temperature",
        RegisterBank.INPUT,
        Measurement.TEMPERATURE,
        type=ValueType.S16,
        scale=0.1,
        unit=C,
    ),
    RegisterDef(
        "min_cell_temperature",
        104,
        "Min Cell Temperature",
        RegisterBank.INPUT,
        Measurement.TEMPERATURE,
        type=ValueType.S16,
        scale=0.1,
        unit=C,
    ),
    RegisterDef(
        "battery_cycle_count",
        106,
        "Battery Cycle Count",
        RegisterBank.INPUT,
        Measurement.COUNT,
    ),
    RegisterDef(
        "home_consumption",
        170,
        "Home Consumption",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
    ),
    RegisterDef(
        "home_consumption_today",
        171,
        "Home Consumption Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
    ),
    RegisterDef(
        "home_consumption_total",
        172,
        "Home Consumption Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
    ),
    # Generator (model-specific, default off)
    RegisterDef(
        "generator_voltage",
        121,
        "Generator Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "generator_frequency",
        122,
        "Generator Frequency",
        RegisterBank.INPUT,
        Measurement.FREQUENCY,
        scale=0.01,
        unit=HZ,
        enabled_default=False,
    ),
    RegisterDef(
        "generator_power",
        123,
        "Generator Power",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "generator_energy_today",
        124,
        "Generator Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "generator_energy_total",
        125,
        "Generator Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    # EPS split-phase power (model-specific, default off)
    RegisterDef(
        "eps_l1_power",
        129,
        "EPS L1 Watts",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "eps_l2_power",
        130,
        "EPS L2 Watts",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    # Per-phase grid (US split-phase, default off)
    RegisterDef(
        "grid_voltage_l1n",
        193,
        "Grid L1 Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "grid_voltage_l2n",
        194,
        "Grid L2 Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    # --- Extended / model-specific input registers ---------------------------
    #
    # Confirmed against the spec's input table (Table 7) and surfaced by
    # capture-based discovery on real hardware: 3-phase (S/T), US split-phase
    # (L1N/L2N), generator, AC-couple, EPS split, extra PV (4-6) and a few
    # diagnostics. Single-phase units report noise here, so all ship
    # default-off until confirmed on the relevant hardware.
    RegisterDef(
        "bat_voltage_sample",
        107,
        "Battery Voltage (Inverter Sample)",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "temperature_t1",
        108,
        "Temperature T1",
        RegisterBank.INPUT,
        Measurement.TEMPERATURE,
        scale=0.1,
        unit=C,
        enabled_default=False,
    ),
    RegisterDef(
        "on_grid_load_power",
        114,
        "On-Grid Load Power",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    # Ten-character ASCII serial across 115-119 (SN[0..9], low byte first).
    RegisterDef("serial_number", 115, "Serial Number", RegisterBank.INPUT, type=ValueType.ASCII, length=5),
    RegisterDef(
        "half_bus_voltage",
        120,
        "Half Bus Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "eps_voltage_l1n",
        127,
        "EPS L1N Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "eps_voltage_l2n",
        128,
        "EPS L2N Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_power_s",
        180,
        "Inverter Power S",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_power_t",
        181,
        "Inverter Power T",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "rectify_power_s", 182, "Rectify Power S", RegisterBank.INPUT, Measurement.POWER, unit=W, enabled_default=False
    ),
    RegisterDef(
        "rectify_power_t", 183, "Rectify Power T", RegisterBank.INPUT, Measurement.POWER, unit=W, enabled_default=False
    ),
    RegisterDef(
        "power_to_grid_s", 184, "Power To Grid S", RegisterBank.INPUT, Measurement.POWER, unit=W, enabled_default=False
    ),
    RegisterDef(
        "power_to_grid_t", 185, "Power To Grid T", RegisterBank.INPUT, Measurement.POWER, unit=W, enabled_default=False
    ),
    RegisterDef(
        "power_from_grid_s",
        186,
        "Power From Grid S",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "power_from_grid_t",
        187,
        "Power From Grid T",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "generator_power_s",
        188,
        "Generator Power S",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "generator_power_t",
        189,
        "Generator Power T",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_current_s",
        190,
        "Inverter Current S",
        RegisterBank.INPUT,
        Measurement.CURRENT,
        scale=0.01,
        unit=A,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_current_t",
        191,
        "Inverter Current T",
        RegisterBank.INPUT,
        Measurement.CURRENT,
        scale=0.01,
        unit=A,
        enabled_default=False,
    ),
    RegisterDef(
        "power_factor_s",
        192,
        "Power Factor S",
        RegisterBank.INPUT,
        Measurement.POWER_FACTOR,
        transform="power_factor",
        enabled_default=False,
    ),
    RegisterDef(
        "generator_voltage_l1n",
        195,
        "Generator L1N Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "generator_voltage_l2n",
        196,
        "Generator L2N Voltage",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_power_l1n",
        197,
        "Inverter Power L1N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "inverter_power_l2n",
        198,
        "Inverter Power L2N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "rectify_power_l1n",
        199,
        "Rectify Power L1N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "rectify_power_l2n",
        200,
        "Rectify Power L2N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "power_to_grid_l1n",
        201,
        "Power To Grid L1N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "power_to_grid_l2n",
        202,
        "Power To Grid L2N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "power_from_grid_l1n",
        203,
        "Power From Grid L1N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "power_from_grid_l2n",
        204,
        "Power From Grid L2N",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "power_factor_t",
        205,
        "Power Factor T",
        RegisterBank.INPUT,
        Measurement.POWER_FACTOR,
        transform="power_factor",
        enabled_default=False,
    ),
    RegisterDef(
        "ac_couple_power_s",
        206,
        "AC Couple Power S",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "ac_couple_power_t",
        207,
        "AC Couple Power T",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "on_grid_load_power_s",
        208,
        "On-Grid Load Power S",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "on_grid_load_power_t",
        209,
        "On-Grid Load Power T",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
    RegisterDef(
        "pv4_voltage",
        217,
        "Solar Voltage Array 4",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "pv5_voltage",
        218,
        "Solar Voltage Array 5",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "pv6_voltage",
        219,
        "Solar Voltage Array 6",
        RegisterBank.INPUT,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        enabled_default=False,
    ),
    RegisterDef(
        "pv4_power", 220, "Solar Output Array 4", RegisterBank.INPUT, Measurement.POWER, unit=W, enabled_default=False
    ),
    RegisterDef(
        "pv5_power", 221, "Solar Output Array 5", RegisterBank.INPUT, Measurement.POWER, unit=W, enabled_default=False
    ),
    RegisterDef(
        "pv6_power", 222, "Solar Output Array 6", RegisterBank.INPUT, Measurement.POWER, unit=W, enabled_default=False
    ),
    RegisterDef(
        "pv4_energy_today",
        223,
        "Solar Array 4 Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "pv4_energy_total",
        224,
        "Solar Array 4 Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "pv5_energy_today",
        226,
        "Solar Array 5 Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "pv5_energy_total",
        227,
        "Solar Array 5 Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "pv6_energy_today",
        229,
        "Solar Array 6 Daily",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "pv6_energy_total",
        230,
        "Solar Array 6 Total",
        RegisterBank.INPUT,
        Measurement.ENERGY,
        type=ValueType.U32,
        scale=0.1,
        unit=KWH,
        enabled_default=False,
    ),
    RegisterDef(
        "smart_load_power",
        232,
        "Smart Load Power",
        RegisterBank.INPUT,
        Measurement.POWER,
        unit=W,
        enabled_default=False,
    ),
)


# --- Hold registers (settings → numbers) -------------------------------------

HOLD_REGISTERS: tuple[RegisterDef, ...] = (
    RegisterDef(
        "start_pv_voltage",
        22,
        "Start PV Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=90.0,
        value_max=500.0,
        enabled_default=False,
    ),
    RegisterDef(
        "grid_volt_conn_low",
        25,
        "Grid Volt Connect Low",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        enabled_default=False,
    ),
    RegisterDef(
        "grid_volt_conn_high",
        26,
        "Grid Volt Connect High",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        enabled_default=False,
    ),
    RegisterDef(
        "grid_freq_conn_low",
        27,
        "Grid Freq Connect Low",
        RegisterBank.HOLD,
        Measurement.FREQUENCY,
        scale=0.01,
        unit=HZ,
        writable=True,
        enabled_default=False,
    ),
    RegisterDef(
        "grid_freq_conn_high",
        28,
        "Grid Freq Connect High",
        RegisterBank.HOLD,
        Measurement.FREQUENCY,
        scale=0.01,
        unit=HZ,
        writable=True,
        enabled_default=False,
    ),
    RegisterDef(
        "system_charge_rate",
        64,
        "System Charge Power Rate",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "system_discharge_rate",
        65,
        "System Discharge Power Rate",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "ac_charge_rate",
        66,
        "AC Charge Power Rate",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "ac_charge_soc_limit",
        67,
        "AC Battery Charge Level",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "charge_priority_rate",
        74,
        "Priority Charge Rate",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "charge_priority_soc_limit",
        75,
        "Priority Charge Level",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "forced_discharge_rate",
        82,
        "Forced Discharge Power Rate",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "forced_discharge_soc_limit",
        83,
        "Forced Discharge Battery Level",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "charge_voltage",
        99,
        "Charge Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=50.0,
        value_max=59.0,
        enabled_default=False,
    ),
    RegisterDef(
        "discharge_cutoff_voltage",
        100,
        "Discharge Cut-off Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=40.0,
        value_max=52.0,
        enabled_default=False,
    ),
    RegisterDef(
        "charge_current_limit",
        101,
        "Charge Current Limit",
        RegisterBank.HOLD,
        Measurement.CURRENT,
        unit=A,
        writable=True,
        value_min=0,
        value_max=140,
    ),
    RegisterDef(
        "discharge_current_limit",
        102,
        "Discharge Current Limit",
        RegisterBank.HOLD,
        Measurement.CURRENT,
        unit=A,
        writable=True,
        value_min=0,
        value_max=140,
    ),
    RegisterDef(
        "feed_in_grid_power_rate",
        103,
        "Feed-in Grid Power",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
    ),
    RegisterDef(
        "discharge_cutoff_soc",
        105,
        "On-grid Discharge Cut-off SOC",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=10,
        value_max=90,
    ),
    # --- Extended hold registers (spec Table 8) ------------------------------
    #
    # Battery / charge / discharge settings beyond the validated core, plus the
    # read-only firmware code. All ship default-off and bounded to the spec's
    # range; the regulatory grid-protection block (29-63), the 48-slot
    # OptimalChg bit schedule (125-131) and LCD/meter/bootloader diagnostics are
    # deliberately not mapped (see docs/discovered-registers.md).
    #
    # Firmware/model code: FWCode0..3, two ASCII chars per register (7-8).
    RegisterDef(
        "firmware_code",
        7,
        "Firmware Code",
        RegisterBank.HOLD,
        type=ValueType.ASCII,
        length=2,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_cell_voltage_low",
        132,
        "Battery Cell Voltage Lower Limit",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=0.0,
        value_max=20.0,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_cell_voltage_high",
        133,
        "Battery Cell Voltage Upper Limit",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=0.0,
        value_max=20.0,
        enabled_default=False,
    ),
    RegisterDef(
        "float_charge_voltage",
        144,
        "Float Charge Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=50.0,
        value_max=56.0,
        enabled_default=False,
    ),
    RegisterDef(
        "battery_nominal_voltage",
        148,
        "Battery Nominal Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=40.0,
        value_max=59.0,
        enabled_default=False,
    ),
    RegisterDef(
        "ac_charge_start_voltage",
        158,
        "AC Charge Start Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=38.5,
        value_max=52.0,
        enabled_default=False,
    ),
    RegisterDef(
        "ac_charge_end_voltage",
        159,
        "AC Charge End Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=48.0,
        value_max=59.0,
        enabled_default=False,
    ),
    RegisterDef(
        "ac_charge_start_soc",
        160,
        "AC Charge Start SOC",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=90,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_low_voltage",
        162,
        "Battery Low Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=40.0,
        value_max=50.0,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_low_back_voltage",
        163,
        "Battery Low Recovery Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=42.0,
        value_max=52.0,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_low_soc",
        164,
        "Battery Low SOC",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=90,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_low_back_soc",
        165,
        "Battery Low Recovery SOC",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=20,
        value_max=100,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_low_to_utility_voltage",
        166,
        "Battery Low to Grid Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=44.4,
        value_max=51.4,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_low_to_utility_soc",
        167,
        "Battery Low to Grid SOC",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=0,
        value_max=100,
        enabled_default=False,
    ),
    RegisterDef(
        "ac_charge_battery_current",
        168,
        "AC Charge Battery Current",
        RegisterBank.HOLD,
        Measurement.CURRENT,
        unit=A,
        writable=True,
        value_min=0,
        value_max=140,
        enabled_default=False,
    ),
    RegisterDef(
        "ongrid_eod_voltage",
        169,
        "On-grid End of Discharge Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=40.0,
        value_max=56.0,
        enabled_default=False,
    ),
    RegisterDef(
        "max_gen_charge_battery_current",
        198,
        "Max Generator Charge Current",
        RegisterBank.HOLD,
        Measurement.CURRENT,
        unit=A,
        writable=True,
        value_min=0,
        value_max=4000,
        enabled_default=False,
    ),
    RegisterDef(
        "charge_priority_voltage",
        201,
        "Charge Priority Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=48.0,
        value_max=59.0,
        enabled_default=False,
    ),
    RegisterDef(
        "forced_discharge_voltage",
        202,
        "Forced Discharge Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=40.0,
        value_max=56.0,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_stop_charge_soc",
        227,
        "Battery Stop Charge SOC",
        RegisterBank.HOLD,
        Measurement.PERCENT,
        unit=PCT,
        writable=True,
        value_min=10,
        value_max=101,
        enabled_default=False,
    ),
    RegisterDef(
        "bat_stop_charge_voltage",
        228,
        "Battery Stop Charge Voltage",
        RegisterBank.HOLD,
        Measurement.VOLTAGE,
        scale=0.1,
        unit=V,
        writable=True,
        value_min=40.0,
        value_max=59.5,
        enabled_default=False,
    ),
)


# --- Flag (bit-packed) hold registers (switches) -----------------------------
#
# Register 21 (FuncEn) and 110 (FunctionEn1) bit assignments are taken verbatim
# from the spec's hold-register table.

FLAG_REGISTERS: tuple[FlagRegister, ...] = (
    FlagRegister(
        "func_en",
        21,
        "Function Enable",
        (
            FlagDef(0, "eps_enable", "Off-grid (EPS) Enable"),
            FlagDef(1, "ovf_load_derate_enable", "OVF Load Derate Enable"),
            FlagDef(2, "drms_enable", "DRMS Enable"),
            FlagDef(3, "lvrt_enable", "Low Voltage Ride Through"),
            FlagDef(4, "anti_island_enable", "Anti Island Enable"),
            FlagDef(5, "neutral_detect_enable", "Neutral Detect Enable"),
            FlagDef(6, "grid_on_power_ss_enable", "Grid On Power Soft Start"),
            FlagDef(7, "ac_charge_enable", "AC Charge Enable"),
            FlagDef(8, "seamless_eps_switching", "Seamless EPS Switching"),
            FlagDef(9, "set_to_standby", "Normal / Standby"),
            FlagDef(10, "forced_discharge_enable", "Force Discharge Enable"),
            FlagDef(11, "forced_charge_enable", "Force Charge Enable"),
            FlagDef(12, "iso_enable", "ISO Enable"),
            FlagDef(13, "gfci_enable", "GFCI Enable"),
            FlagDef(14, "dci_enable", "DCI Enable"),
            FlagDef(15, "feed_in_grid", "Feed-In Grid"),
        ),
    ),
    FlagRegister(
        "function_en1",
        110,
        "Function Enable 1",
        (
            FlagDef(0, "pv_grid_off_enable", "PV Grid Off Enable"),
            FlagDef(1, "fast_zero_export", "Fast Zero Export"),
            FlagDef(2, "micro_grid_enable", "Micro Grid Enable"),
            FlagDef(3, "battery_shared", "Battery Shared"),
            FlagDef(4, "charge_last", "Charge Last"),
            FlagDef(7, "buzzer_enable", "Buzzer Enable"),
            FlagDef(10, "take_load_together", "Take Load Together"),
            # Bit 11 (on-grid working mode) is exposed as a select, not a switch.
            FlagDef(14, "green_mode_enable", "Green Mode Enable"),
            FlagDef(15, "eco_mode_enable", "Eco Mode Enable"),
        ),
    ),
)


# --- Time (HH:MM) hold registers (schedule slots → time entities) ------------
#
# Each charge/discharge schedule edge is one register packing hour (low byte)
# and minute (high byte). Slot 1 of the three single-phase families ships
# enabled; the extra slots stay off until a user needs multiple windows.

TIME_REGISTERS: tuple[TimeRegister, ...] = (
    TimeRegister("ac_charge_start", 68, "AC Charge Start"),
    TimeRegister("ac_charge_end", 69, "AC Charge End"),
    TimeRegister("ac_charge_start_2", 70, "AC Charge Start 2", enabled_default=False),
    TimeRegister("ac_charge_end_2", 71, "AC Charge End 2", enabled_default=False),
    TimeRegister("ac_charge_start_3", 72, "AC Charge Start 3", enabled_default=False),
    TimeRegister("ac_charge_end_3", 73, "AC Charge End 3", enabled_default=False),
    TimeRegister("charge_priority_start", 76, "Charge Priority Start"),
    TimeRegister("charge_priority_end", 77, "Charge Priority End"),
    TimeRegister("charge_priority_start_2", 78, "Charge Priority Start 2", enabled_default=False),
    TimeRegister("charge_priority_end_2", 79, "Charge Priority End 2", enabled_default=False),
    TimeRegister("charge_priority_start_3", 80, "Charge Priority Start 3", enabled_default=False),
    TimeRegister("charge_priority_end_3", 81, "Charge Priority End 3", enabled_default=False),
    TimeRegister("forced_discharge_start", 84, "Forced Discharge Start"),
    TimeRegister("forced_discharge_end", 85, "Forced Discharge End"),
    TimeRegister("forced_discharge_start_2", 86, "Forced Discharge Start 2", enabled_default=False),
    TimeRegister("forced_discharge_end_2", 87, "Forced Discharge End 2", enabled_default=False),
    TimeRegister("forced_discharge_start_3", 88, "Forced Discharge Start 3", enabled_default=False),
    TimeRegister("forced_discharge_end_3", 89, "Forced Discharge End 3", enabled_default=False),
)


# --- Select (enumerated) hold registers (→ select entities) ------------------
#
# On-grid working mode is FunctionEn1 (register 110) bit 11: 0 = self
# consumption, 1 = charge first.

SELECT_REGISTERS: tuple[SelectRegister, ...] = (
    SelectRegister(
        "on_grid_working_mode",
        110,
        "On Grid Working Mode",
        ("Self-Consumption", "Charge-First"),
        mask=1 << 11,
    ),
    # Whole-register enums from spec Table 8 (default-off until confirmed per model).
    SelectRegister(
        "output_priority",
        145,
        "Output Priority",
        ("Battery First", "PV First", "AC First"),
        enabled_default=False,
    ),
    SelectRegister(
        "line_mode",
        146,
        "Line Mode",
        ("APL", "UPS", "GEN"),
        enabled_default=False,
    ),
    SelectRegister(
        "grid_type",
        205,
        "Grid Type",
        ("Split 240V/120V", "3-Phase 208V/120V", "Single 240V", "Single 230V", "Split 200V/100V"),
        enabled_default=False,
    ),
)


# --- Lookups -----------------------------------------------------------------

_INPUT_BY_KEY = {d.key: d for d in INPUT_REGISTERS}
_HOLD_BY_KEY = {d.key: d for d in HOLD_REGISTERS}


def find_input(key: str) -> RegisterDef | None:
    """Return the input register definition with ``key``, or None."""
    return _INPUT_BY_KEY.get(key)


def find_hold(key: str) -> RegisterDef | None:
    """Return the hold register definition with ``key``, or None."""
    return _HOLD_BY_KEY.get(key)


def mapped_input_addresses() -> frozenset[int]:
    """All input addresses consumed by the map (incl. high words of 32-bit values).

    Discovery diffs observed input addresses against this set.
    """
    return frozenset(a for d in INPUT_REGISTERS for a in d.addresses())


def mapped_hold_addresses() -> frozenset[int]:
    """All hold addresses consumed by the map (scalar settings and flag registers).

    Discovery diffs observed hold addresses against this set.
    """
    scalar = {a for d in HOLD_REGISTERS for a in d.addresses()}
    flags = {f.address for f in FLAG_REGISTERS}
    times = {t.address for t in TIME_REGISTERS}
    selects = {s.address for s in SELECT_REGISTERS}
    return frozenset(scalar | flags | times | selects)
