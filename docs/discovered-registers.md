# Discovered registers

This records registers found by **capture-based discovery** against a real
inverter, what was added to the map, and — importantly — the ones we have seen
but are **not yet confident enough to map**.

## How these were found

A passive `tcpdump` of the dongle's local poller (captured on an OPNsense router,
see [`capturing-packets.md`](capturing-packets.md)) was decoded with
`scripts/analyze_capture.py` and diffed against the register map. The poller
reads the **input bank** across addresses **0–380**; cross-referencing the
non-zero unmapped addresses against the spec's input table (Table 7) gave the
additions below.

- **Hardware:** single-phase hybrid, BA-series dongle (the project's validated
  primary). 3-phase, US split-phase, generator and extra-PV values are noise on
  this unit, which is exactly why those mapped registers ship disabled.
- **Byte order:** little-endian throughout; ASCII strings are two chars per
  register, low byte first (e.g. the serial decoded cleanly that way).

## Newly mapped (input bank)

Confirmed against the spec and added to `INPUT_REGISTERS`. All ship
`enabled_default=False` **except** `serial_number` — single-phase units report
noise for the model-specific ones, and defaulting them off also stops discovery
re-flagging them. Re-enable per platform once confirmed on the relevant hardware.

| Addr | Key | Spec name | Decode | Family |
| --- | --- | --- | --- | --- |
| 107 | `bat_voltage_sample` | BatVoltSample_INV | 0.1 V | Battery diag |
| 108 | `temperature_t1` | T1 | 0.1 °C | Temperature (12k) |
| 114 | `on_grid_load_power` | OnGridloadPower | W | Load (12k) |
| 115–119 | `serial_number` | SN[0..9] | ASCII (10 chars) | **Identity (enabled)** |
| 120 | `half_bus_voltage` | VBusP | 0.1 V | Bus diag |
| 127 / 128 | `eps_voltage_l1n` / `_l2n` | EPSVoltL1N / L2N | 0.1 V | EPS split-phase |
| 180–187 | `inverter_power_s/t`, `rectify_power_s/t`, `power_to_grid_s/t`, `power_from_grid_s/t` | Pinv/Prec/Ptogrid/Ptouser S,T | W | 3-phase S/T |
| 188 / 189 | `generator_power_s` / `_t` | GenPower_S / T | W | Generator 3-phase |
| 190 / 191 | `inverter_current_s` / `_t` | IinvRMS_S / T | 0.01 A | 3-phase S/T |
| 192 / 205 | `power_factor_s` / `_t` | PF_S / PF_T | ×0.001 folded | 3-phase S/T |
| 195 / 196 | `generator_voltage_l1n` / `_l2n` | GenVoltL1N / L2N | 0.1 V | Generator US |
| 197–204 | `inverter_power_l1n/l2n`, `rectify_power_l1n/l2n`, `power_to_grid_l1n/l2n`, `power_from_grid_l1n/l2n` | Pinv/Prec/Ptogrid/Ptouser L1N,L2N | W | US split-phase |
| 206 / 207 | `ac_couple_power_s` / `_t` | ACCouplePower_S / T | W | AC-couple |
| 208 / 209 | `on_grid_load_power_s` / `_t` | OnGridloadPowerS / T | W | Load 3-phase |
| 217–222 | `pv4/5/6_voltage`, `pv4/5/6_power` | Vpv4-6 / Ppv4-6 | 0.1 V / W | Extra PV |
| 223–230 | `pv4/5/6_energy_today`, `pv4/5/6_energy_total` | Epv4-6_day / _all | 0.1 kWh (total = U32) | Extra PV |
| 232 | `smart_load_power` | Smart Load Power | W | Smart load |

## Seen but NOT mapped — needs confirmation

These showed **non-zero** values in the capture but are deliberately left out of
the map: either the encoding is bit-packed/enumerated (low value as a plain
sensor), or the spec's scale is ambiguous enough that mapping it would publish
**wrong** data. Documented here so the next person doesn't re-discover them cold.

| Addr | Spec name | Observed (raw) | Why unsure | To confirm |
| --- | --- | --- | --- | --- |
| 78 | (flags block ~76–79) | `0x0002` | Bit-packed status: AC input type, AC-couple flow/enable, smart-load flow/enable, EPS/Grid/Pload power-display flags. The spec's register↔bit alignment across 76–79 is hard to read. | Map the exact bits per register; verify against the spec's flag table for this model. |
| 79 | (flags block ~76–79) | `0x0101` | As above — bit-packed display/flow flags. | As above. |
| 80 | BatTypeAndBrand / BatComType | `0x001a` | Enum from the model-definition file (battery type/brand) plus a `BatComType` bit (0=CAN, 1=485). Meaning is per-model. | Obtain the model-definition mapping; expose `BatComType` as a flag and the type as an enum. |
| 95 | BatStatus_INV | `0x0003` | "Inverter aggregates lithium battery status" — an enum/bitfield whose meaning depends on the BMS. | Confirm bit/enum meanings from the BMS status definition. |
| 113 | MasterOrSlave / SingleOrThreePhase / Phases / ParallelNum | `0x01F5` | Packed parallel/phase status: master-or-slave (bit0–1), phase R/S/T (bit2–3), phase sequence (bit4–5), inverters-in-parallel (bit8–15). | Decode as a multi-field bitfield; verify field positions on a parallel setup. |
| 210 | Remaining seconds | `0x0000` | One-click-charge countdown; read 0 (inactive) so the scale/behaviour wasn't observed. | Capture while a one-click charge is running; map as a duration (s). |
| 213 | *(unlabelled in spec)* | `0x021C` (540) | No clear label in the spec input table between 210 and 214. | Identify from a newer spec revision or vendor confirmation. |
| 214 | uwNTCForINDC | `0x01E0` (480) | Spec lists "celsius", but **480 is implausible as °C** — likely raw NTC/ADC counts or a non-unit scale. | Determine the true scale (compare against a known temperature); do **not** map as direct °C. |
| 215 | uwNTCForDCDCL | `0x005A` (90) | Same family as 214 — scale unconfirmed. | As above. |
| 216 | uwNTCForDCDCH | `0x003C` (60) | Same family as 214 — scale unconfirmed. | As above. |

Beyond these, the inverter answers reads across the whole input range but the
remaining addresses (roughly 225–380, plus assorted gaps) read **0** on this
model — reserved/unused here, not worth mapping without a unit that reports them.

## Hold register coverage (spec Table 8)

Hold registers 7–261 are defined in the spec's Table 8. The map now covers the
validated core plus the high-value battery/charge/discharge settings:
identity (`firmware_code`, regs 7–8 ASCII), cell/float/nominal voltages, AC-charge
and battery-low voltage/SOC thresholds, charge/forced-discharge voltage limits,
charge currents, and three whole-register selects (`output_priority` 145,
`line_mode` 146, `grid_type` 205). Everything added here ships
`enabled_default=False` and is bounded to the spec's range.

### Live hold-bank sweep

A read-only sweep of holds 0–279 on a real inverter validated the map (the
selects in particular: `grid_type` → "Single 230V", `output_priority` →
"Battery First", `line_mode` → "APL"). It also turned up two things:

- **Spec ranges are nominal, not hard limits.** The device reported values
  *outside* the spec's "Range" column: `charge_current_limit` 146 (spec 0–140),
  `feed_in_grid_power_rate` 120 (spec 0–100), `discharge_cutoff_soc` 5 (spec
  10–90). Bounds were widened (currents → 0–200, feed-in → 0–150, cutoff SOC →
  0–90) so `encode_value` accepts the inverter's own values. Treat spec ranges as
  guidance, not ceilings.
- **More registers mapped from live values** (scales confirmed by what they
  returned, all `enabled_default=False`): lead-acid temperature limits 106–109
  (signed, 0.1 °C — the discharge lower limit read −20.0 °C), generator-charge
  194–197, smart-load 213–216, AC-couple 220–223.
- `firmware_code` (7–8) read **zero** on this unit — the ASCII mapping is mapped
  but unconfirmed against a populated value.

Deliberately **not** mapped (documented so the next person doesn't map them
cold):

- **Regulatory grid protection (29–63)** — `GridVoltLimit*` / `GridFreqLimit*`
  trip points and times. Region-locked ("according to regulatory requirements")
  and risky to expose as user-writable; map per regulation if ever needed.
- **OptimalChg/DisChg schedule (125–131)** — 48 two-bit time slots packed across
  registers; needs a dedicated multi-field decoder, not the current helpers.
- **Power-export CMDs (60–63, 138–143)** — duplicate the percentage settings
  already mapped at 64–66/74/82 at finer resolution; left out to avoid two
  entities for one knob.
- **LCD / meter / WattNode / bootloader / parallel-system config (179, 224–252)**
  — installer/diagnostic registers, mostly bit-packed and model-specific.

## Follow-up candidates

- The bit-packed status registers above (78–80, 95, 113) would be a natural fit
  for the `FlagRegister` / `SelectRegister` mechanisms once the bit layouts are
  confirmed.
- `uFunctionEn2` (179) and `uFunction4En` (232–233) are further bit-packed
  function-enable registers (peak-shaving, smart-load, AC-couple, working-mode
  selection); map as `FlagRegister`s once the per-model bit meanings are pinned.
- Remaining well-defined settings the sweep surfaced but left unmapped:
  generator current/times (198, 255–259), peak-shaving (206–212, 217–219),
  freq-derate (115–118, 134–136) and the VoltWatt / Vref Q-P curve (180–197).
  Add the same way (default-off, spec-bounded) when a platform needs them.
