# Changelog

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
