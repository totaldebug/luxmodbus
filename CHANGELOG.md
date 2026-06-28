# Changelog

## Unreleased

### Added

- Request builders on `DataFrame`: `read_input`, `read_hold`, `write_single`,
  and `write_multi` construct read/write data frames (the inner frame is wrapped
  in a `Frame` envelope by the caller).
- `decode_read_response` turns a decoded read (or single-write echo) response
  into the `{address: word}` map consumed by `decode_inputs` /
  `DiscoveryStore.observe_many`. `scripts/analyze_capture.py` now uses it instead
  of re-implementing the unpacking inline.
- `encode_value` — the inverse of `decode_value` for numeric registers: enforces
  the declared `value_min` / `value_max` bounds and the register scale, returning
  the raw word to write.
