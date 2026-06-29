"""Tests for luxmodbus.registers — decode engine and map invariants."""

from __future__ import annotations

import pytest

from luxmodbus.registers import (
    FLAG_REGISTERS,
    HOLD_REGISTERS,
    INPUT_REGISTERS,
    SELECT_REGISTERS,
    TIME_REGISTERS,
    ByteSelect,
    Measurement,
    RegisterBank,
    ValueType,
    decode_flags,
    decode_holds,
    decode_inputs,
    decode_select,
    decode_time,
    decode_value,
    encode_time,
    encode_value,
    find_hold,
    find_input,
    mapped_hold_addresses,
    mapped_input_addresses,
    set_flag,
    set_select,
)

# --- Scalar decoding ---------------------------------------------------------


def test_voltage_scale():
    pv1 = find_input("pv1_voltage")
    assert decode_value(pv1, {1: 2503}) == 250.3


def test_frequency_scale():
    fac = find_input("grid_frequency")
    assert decode_value(fac, {15: 5001}) == 50.01


def test_unit_scale_returns_int():
    # Power has scale 1.0 -> value stays an int, not 123.0.
    ppv1 = find_input("pv1_power")
    value = decode_value(ppv1, {7: 123})
    assert value == 123
    assert isinstance(value, int)


def test_missing_register_is_none():
    assert decode_value(find_input("pv1_voltage"), {}) is None


# --- Encoding (write path) ---------------------------------------------------


def test_encode_value_applies_inverse_scale():
    cv = find_hold("charge_voltage")
    assert encode_value(cv, 53.0) == 530  # 53.0 V / 0.1


def test_encode_value_unit_scale_is_identity():
    rate = find_hold("system_charge_rate")
    assert encode_value(rate, 75) == 75


def test_encode_value_round_trips_decode():
    cv = find_hold("charge_voltage")
    assert decode_value(cv, {cv.address: encode_value(cv, 53.0)}) == 53.0


def test_encode_value_signed_round_trips():
    # battery_current is s16 @ 0.01 A; -1.0 A -> -100 -> two's-complement word.
    current = find_input("battery_current")
    word = encode_value(current, -1.0)
    assert decode_value(current, {current.address: word}) == -1.0


@pytest.mark.parametrize("value", [49.0, 60.0])
def test_encode_value_rejects_out_of_range(value):
    with pytest.raises(ValueError, match="charge_voltage"):
        encode_value(find_hold("charge_voltage"), value)


@pytest.mark.parametrize("key", ["serial_number", "soc", "power_factor"])
def test_encode_value_rejects_non_numeric(key):
    with pytest.raises(ValueError, match="numeric"):
        encode_value(find_input(key), 1)


def test_encode_value_rejects_32bit():
    with pytest.raises(ValueError, match="32-bit"):
        encode_value(find_input("pv1_energy_total"), 1.0)


# --- Signed values -----------------------------------------------------------


def test_signed_battery_current_negative():
    cur = find_input("battery_current")
    # 0xFFFF as S16 == -1 raw -> -0.01 A
    assert decode_value(cur, {98: 0xFFFF}) == -0.01


def test_signed_cell_temperature_negative():
    tmin = find_input("min_cell_temperature")
    # -50 raw (0.1C) -> -5.0 C
    assert decode_value(tmin, {104: 0x10000 - 50}) == -5.0


# --- 32-bit, little-word-first ----------------------------------------------


def test_u32_combines_low_then_high():
    total = find_input("pv1_energy_total")
    assert total.type is ValueType.U32
    assert total.addresses() == (40, 41)
    # low=0x0001 high=0x0002 -> 0x00020001 = 131073, * 0.1 = 13107.3
    assert decode_value(total, {40: 1, 41: 2}) == round(131073 * 0.1, 3)


def test_u32_requires_both_words():
    total = find_input("pv1_energy_total")
    assert decode_value(total, {40: 1}) is None


# --- Byte-packed register 5 (SOC low / SOH high) -----------------------------


def test_soc_and_soh_share_register_5():
    soc = find_input("soc")
    soh = find_input("soh")
    assert soc.address == soh.address == 5
    raw = {5: (95 << 8) | 87}  # high byte 95, low byte 87
    assert decode_value(soc, raw) == 87
    assert decode_value(soh, raw) == 95
    assert soc.byte is ByteSelect.LOW
    assert soh.byte is ByteSelect.HIGH


# --- Power factor transform --------------------------------------------------


def test_power_factor_under_and_over():
    pf = find_input("power_factor")
    assert decode_value(pf, {19: 1000}) == 1.0
    assert decode_value(pf, {19: 950}) == 0.95
    assert decode_value(pf, {19: 1100}) == pytest.approx(-0.1)  # (1000-1100)/1000


# --- decode_inputs -----------------------------------------------------------


def test_decode_inputs_only_present_keys():
    out = decode_inputs({1: 2400, 4: 530, 5: (90 << 8) | 88})
    assert out == {"pv1_voltage": 240.0, "battery_voltage": 53.0, "soc": 88, "soh": 90}


# --- Flag registers ----------------------------------------------------------


def test_decode_flags_func_en():
    func_en = next(f for f in FLAG_REGISTERS if f.address == 21)
    # bit7 AC charge + bit15 feed-in grid set.
    value = (1 << 7) | (1 << 15)
    flags = decode_flags(value, func_en)
    assert flags["ac_charge_enable"] is True
    assert flags["feed_in_grid"] is True
    assert flags["eps_enable"] is False


def test_set_flag_round_trip():
    func_en = next(f for f in FLAG_REGISTERS if f.address == 21)
    ac_charge = next(f for f in func_en.flags if f.key == "ac_charge_enable")
    on = set_flag(0, ac_charge, True)
    assert on == (1 << 7)
    assert set_flag(on, ac_charge, False) == 0


def test_set_flag_stays_16_bit():
    func_en = next(f for f in FLAG_REGISTERS if f.address == 21)
    feed_in = next(f for f in func_en.flags if f.key == "feed_in_grid")
    assert set_flag(0xFFFF, feed_in, False) == 0x7FFF


# --- Map invariants ----------------------------------------------------------


def test_input_keys_unique():
    keys = [d.key for d in INPUT_REGISTERS]
    assert len(keys) == len(set(keys))


def test_hold_keys_unique():
    keys = [d.key for d in HOLD_REGISTERS]
    assert len(keys) == len(set(keys))


def test_banks_are_correct():
    assert all(d.bank is RegisterBank.INPUT for d in INPUT_REGISTERS)
    assert all(d.bank is RegisterBank.HOLD for d in HOLD_REGISTERS)


def test_writable_only_on_hold():
    assert all(not d.writable for d in INPUT_REGISTERS)


def test_scaled_registers_have_units():
    # Anything with a real measurement (not enum/none/count) should carry a unit.
    unitless = {
        Measurement.NONE,
        Measurement.ENUM,
        Measurement.COUNT,
        Measurement.POWER_FACTOR,
    }
    for d in INPUT_REGISTERS:
        if d.measurement not in unitless:
            assert d.unit is not None, f"{d.key} missing unit"


def test_mapped_addresses_include_high_words():
    addrs = mapped_input_addresses()
    assert 40 in addrs and 41 in addrs  # pv1_energy_total U32
    assert 5 in addrs


def test_decode_holds():
    out = decode_holds({64: 80, 99: 530})
    assert out["system_charge_rate"] == 80  # scale 1
    assert out["charge_voltage"] == 53.0  # 530 * 0.1


def test_find_hold_lookup():
    rate = find_hold("system_charge_rate")
    assert rate is not None
    assert rate.address == 64
    assert rate.writable is True
    assert find_hold("nonexistent") is None


def test_value_min_max_ordered():
    for d in HOLD_REGISTERS:
        if d.value_min is not None and d.value_max is not None:
            assert d.value_min <= d.value_max


# --- Time registers ----------------------------------------------------------


def test_decode_time_low_hour_high_minute():
    # register packs hour in the low byte, minute in the high byte.
    assert decode_time((45 << 8) | 9) == (9, 45)  # 09:45


def test_encode_time_round_trip():
    for hour, minute in ((0, 0), (9, 45), (23, 59)):
        assert decode_time(encode_time(hour, minute)) == (hour, minute)


def test_ac_charge_slot_one_enabled_by_default():
    start = next(t for t in TIME_REGISTERS if t.key == "ac_charge_start")
    assert start.address == 68
    assert start.enabled_default is True
    slot2 = next(t for t in TIME_REGISTERS if t.key == "ac_charge_start_2")
    assert slot2.enabled_default is False


def test_time_keys_unique_and_addresses_mapped():
    keys = [t.key for t in TIME_REGISTERS]
    assert len(keys) == len(set(keys))
    mapped = mapped_hold_addresses()
    assert all(t.address in mapped for t in TIME_REGISTERS)


# --- Select registers --------------------------------------------------------


def test_on_grid_working_mode_select_bit11():
    select = next(s for s in SELECT_REGISTERS if s.key == "on_grid_working_mode")
    assert select.address == 110
    assert select.options == ("Self-Consumption", "Charge-First")
    # bit 11 clear -> first option; set -> second.
    assert decode_select(0, select) == "Self-Consumption"
    assert decode_select(1 << 11, select) == "Charge-First"


def test_set_select_preserves_other_bits():
    select = next(s for s in SELECT_REGISTERS if s.key == "on_grid_working_mode")
    # Other bits (e.g. bit 0) must survive a write to the select's field.
    assert set_select(0b1, select, "Charge-First") == (1 << 11) | 0b1
    assert set_select((1 << 11) | 0b1, select, "Self-Consumption") == 0b1


def test_select_address_mapped_for_discovery():
    assert all(s.address in mapped_hold_addresses() for s in SELECT_REGISTERS)


def test_on_grid_working_mode_not_a_switch_flag():
    function_en1 = next(f for f in FLAG_REGISTERS if f.address == 110)
    assert all(flag.key != "on_grid_working_mode" for flag in function_en1.flags)


def test_extended_select_round_trips():
    grid_type = next(s for s in SELECT_REGISTERS if s.key == "grid_type")
    assert decode_select(set_select(0, grid_type, "Single 230V"), grid_type) == "Single 230V"
    output_priority = next(s for s in SELECT_REGISTERS if s.key == "output_priority")
    assert decode_select(set_select(0, output_priority, "PV First"), output_priority) == "PV First"


def test_extended_selects_default_off():
    new = [s for s in SELECT_REGISTERS if s.key != "on_grid_working_mode"]
    assert new and all(not s.enabled_default for s in new)


# --- Extended hold settings (spec Table 8) -----------------------------------


def test_float_charge_voltage_bounds_and_scale():
    fcv = find_hold("float_charge_voltage")
    assert fcv.address == 144
    assert fcv.writable is True
    assert (fcv.value_min, fcv.value_max) == (50.0, 56.0)
    assert encode_value(fcv, 53.0) == 530  # 53.0 V / 0.1
    assert decode_value(fcv, {144: 530}) == 53.0


@pytest.mark.parametrize("value", [49.9, 56.1])
def test_float_charge_voltage_rejects_out_of_range(value):
    with pytest.raises(ValueError, match="float_charge_voltage"):
        encode_value(find_hold("float_charge_voltage"), value)


def test_firmware_code_decodes_ascii():
    fw = find_hold("firmware_code")
    assert fw.address == 7
    assert fw.writable is False
    assert fw.addresses() == (7, 8)
    # "AAAB" -> reg 7 = 'A','A'; reg 8 = 'A','B' (low byte first).
    raw = {7: (ord("A") << 8) | ord("A"), 8: (ord("B") << 8) | ord("A")}
    assert decode_value(fw, raw) == "AAAB"


def test_extended_hold_settings_default_off():
    extended = {
        "firmware_code",
        "float_charge_voltage",
        "battery_nominal_voltage",
        "bat_low_voltage",
        "bat_stop_charge_voltage",
        "max_gen_charge_battery_current",
    }
    for key in extended:
        assert find_hold(key).enabled_default is False, key


@pytest.mark.parametrize(
    ("key", "observed"),
    [
        ("charge_current_limit", 146),  # spec nominal max was 140
        ("discharge_current_limit", 146),
        ("feed_in_grid_power_rate", 120),  # spec nominal max was 100
        ("discharge_cutoff_soc", 5),  # spec nominal min was 10
    ],
)
def test_relaxed_bounds_accept_live_values(key, observed):
    # Bounds were widened so the inverter's own reported values round-trip.
    defn = find_hold(key)
    assert encode_value(defn, observed) == observed


def test_discovered_gen_charge_voltage():
    defn = find_hold("gen_charge_start_voltage")
    assert defn.address == 194
    assert decode_value(defn, {194: 400}) == 40.0  # live value
    assert encode_value(defn, 40.0) == 400


def test_discovered_lead_acid_temp_is_signed():
    defn = find_hold("lead_acid_temp_lower_discharge")
    assert defn.type is ValueType.S16
    assert decode_value(defn, {106: 0xFF38}) == -20.0  # live value, signed


def test_discovered_smart_load_default_off():
    for key in ("smart_load_on_voltage", "ac_couple_end_voltage", "gen_charge_end_soc"):
        assert find_hold(key).enabled_default is False, key


# --- Grid-support / peak-shaving registers (spec Table 8) --------------------


def test_freq_derate_scale_and_bounds():
    defn = find_hold("over_freq_derate_start")
    assert defn.address == 115
    assert decode_value(defn, {115: 5025}) == 50.25  # live value
    assert encode_value(defn, 50.25) == 5025


def test_volt_watt_curve_percentages_unbounded():
    # The Q(V)/P(Q) curve points have no documented range, so no bounds.
    for key in ("q3_qv", "q4_qv", "p1_qp", "p4_qp"):
        defn = find_hold(key)
        assert defn.value_min is None and defn.value_max is None
        assert defn.writable is True


def test_peak_shaving_power_in_kw():
    defn = find_hold("grid_peak_shaving_power")
    assert defn.address == 206
    assert defn.unit == "kW"
    assert decode_value(defn, {206: 100}) == 10.0  # 0.1 kW scale


@pytest.mark.parametrize(
    ("key", "address"),
    [
        ("peak_shaving_start", 209),
        ("peak_shaving_end_2", 212),
        ("gen_start", 256),
        ("gen_end_2", 259),
    ],
)
def test_new_time_registers(key, address):
    reg = next(t for t in TIME_REGISTERS if t.key == key)
    assert reg.address == address
    assert reg.enabled_default is False
    assert reg.address in mapped_hold_addresses()  # polled & diffed for discovery


# --- Extended model-specific input registers (capture-discovered) ------------


def test_extended_input_registers_decode():
    # Values taken from a real capture (input bank).
    assert decode_value(find_input("bat_voltage_sample"), {107: 524}) == 52.4
    assert decode_value(find_input("half_bus_voltage"), {120: 1876}) == 187.6
    assert decode_value(find_input("inverter_current_s"), {190: 50}) == 0.5
    # PV4 total is a 32-bit little-word-first pair at 224/225.
    pv4_total = find_input("pv4_energy_total")
    assert pv4_total.type is ValueType.U32
    assert pv4_total.addresses() == (224, 225)


def test_extended_input_registers_default_off():
    # All capture-discovered extras are model-specific -> disabled by default.
    for key in ("inverter_power_s", "eps_voltage_l1n", "pv4_voltage", "smart_load_power"):
        assert find_input(key).enabled_default is False


def test_extended_input_addresses_now_mapped():
    # These were flagged UNKNOWN by discovery before being added.
    mapped = mapped_input_addresses()
    assert {107, 120, 180, 192, 217, 232}.issubset(mapped)


def test_power_factor_s_and_t_use_transform():
    assert decode_value(find_input("power_factor_s"), {192: 950}) == 0.95
    assert decode_value(find_input("power_factor_t"), {205: 1100}) == pytest.approx(-0.1)


# --- ASCII serial number -----------------------------------------------------


def test_serial_number_decodes_ascii():
    sn = find_input("serial_number")
    assert sn.type is ValueType.ASCII
    assert sn.word_count == 5
    assert sn.addresses() == (115, 116, 117, 118, 119)
    # Real capture: low byte first, two chars per register.
    raw = {115: 0x3033, 116: 0x3233, 117: 0x3533, 118: 0x3130, 119: 0x3730}
    assert decode_value(sn, raw) == "3032350107"


def test_serial_number_partial_is_none():
    sn = find_input("serial_number")
    assert decode_value(sn, {115: 0x3033}) is None  # missing 116-119


def test_serial_number_strips_padding():
    sn = find_input("serial_number")
    raw = {115: 0x4241, 116: 0x0000, 117: 0x0000, 118: 0x0000, 119: 0x0000}  # "AB" + nulls
    assert decode_value(sn, raw) == "AB"


def test_serial_number_in_decoded_inputs():
    raw = {115: 0x3033, 116: 0x3233, 117: 0x3533, 118: 0x3130, 119: 0x3730}
    assert decode_inputs(raw)["serial_number"] == "3032350107"
