# Changelog

## [0.2.0](https://github.com/totaldebug/luxmodbus/compare/0.1.0...0.2.0) (2026-06-29)


### Features

* Add status-code decoder and enable parity sensors by default ([51610af](https://github.com/totaldebug/luxmodbus/commit/51610af117c97d6223ce713b6b3623b3f9565d15))
* Map grid-support and peak-shaving hold registers (spec Table 8) ([f5801ef](https://github.com/totaldebug/luxmodbus/commit/f5801ef77936d5060538547e8e366c27a67f320e))

## [0.1.0](https://github.com/totaldebug/luxmodbus/compare/0.0.1...0.1.0) (2026-06-28)


### Features

* Add write path — request builders, encode_value, response unpacking ([f1e44fc](https://github.com/totaldebug/luxmodbus/commit/f1e44fc165cc3b29aede5a87164985334ac01911))
* Expand hold-register coverage from spec Table 8 ([048452e](https://github.com/totaldebug/luxmodbus/commit/048452efb6b196a4622423d175b3364be168cc27))
* Initial release ([c15d9ca](https://github.com/totaldebug/luxmodbus/commit/c15d9ca222b4f2c651d545a239ed2678e5d61a50))
* Map live-discovered hold registers, relax bounds to real hardware ([357db71](https://github.com/totaldebug/luxmodbus/commit/357db71eb2f57cd367b972d50310e7243aa91cbb))


### Bug Fixes

* Correct write_multi framing to spec Table 6 layout ([78924bc](https://github.com/totaldebug/luxmodbus/commit/78924bca4d107c6fcd082637a0ff04caecc5dea0))
