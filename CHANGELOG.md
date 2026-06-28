# Changelog

## [0.1.0](https://github.com/totaldebug/luxmodbus/compare/0.0.1...0.1.0) (2026-06-28)


### Features

* Add write path — request builders, encode_value, response unpacking ([f1e44fc](https://github.com/totaldebug/luxmodbus/commit/f1e44fc165cc3b29aede5a87164985334ac01911))
* Expand hold-register coverage from spec Table 8 ([048452e](https://github.com/totaldebug/luxmodbus/commit/048452efb6b196a4622423d175b3364be168cc27))
* Initial release ([c15d9ca](https://github.com/totaldebug/luxmodbus/commit/c15d9ca222b4f2c651d545a239ed2678e5d61a50))
* Map live-discovered hold registers, relax bounds to real hardware ([357db71](https://github.com/totaldebug/luxmodbus/commit/357db71eb2f57cd367b972d50310e7243aa91cbb))


### Bug Fixes

* Correct write_multi framing to spec Table 6 layout ([78924bc](https://github.com/totaldebug/luxmodbus/commit/78924bca4d107c6fcd082637a0ff04caecc5dea0))

## Changelog

## Unreleased

### Added

- Request builders on `DataFrame`: `read_input`, `read_hold`, `write_single`,
  and `write_multi` construct read/write data frames (the inner frame is wrapped
  in a `Frame` envelope by the caller). `read_*` and `write_single` framing is
  confirmed against the spec and the reference implementation that drives real
  dongles; `write_multi` (function 0x10) follows the spec's Table 6 layout
  (`register | count | byte-count | words`) but is **not hardware-verified** —
  real dongles are driven by single writes, so prefer `write_single`.
- `decode_read_response` turns a decoded read (or single-write echo) response
  into the `{address: word}` map consumed by `decode_inputs` /
  `DiscoveryStore.observe_many`. `scripts/analyze_capture.py` now uses it instead
  of re-implementing the unpacking inline.
- `encode_value` — the inverse of `decode_value` for numeric registers: enforces
  the declared `value_min` / `value_max` bounds and the register scale, returning
  the raw word to write.
- Expanded hold-register coverage from the spec's Table 8: the read-only
  `firmware_code` (ASCII), battery cell/float/nominal voltages, AC-charge and
  battery-low voltage/SOC thresholds, charge/forced-discharge voltage limits,
  charge currents, and the `output_priority`, `line_mode` and `grid_type`
  selects. All ship `enabled_default=False` and bounded to the spec range. See
  `docs/discovered-registers.md` for what was deliberately deferred (regulatory
  grid-protection, the OptimalChg bit schedule, and diagnostic/config blocks).
- More hold registers confirmed by a read-only sweep of a live inverter and
  mapped with scales validated against the returned values (all
  `enabled_default=False`): lead-acid temperature limits (106–109, signed),
  generator-charge (194–197), smart-load (213–216), AC-couple (220–223).

### Changed

- Widened `value_min`/`value_max` on `charge_current_limit`,
  `discharge_current_limit` (→ 0–200 A), `feed_in_grid_power_rate` (→ 0–150 %)
  and `discharge_cutoff_soc` (→ 0–90 %): a live inverter reported values outside
  the spec's nominal range, which `encode_value` would otherwise reject. Spec
  ranges are treated as guidance, not hard limits.
