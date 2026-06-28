<a name="readme-top"></a>

[![Release][release-shield]][release-url]
[![Stargazers][stars-shield]][stars-url]
![codecov][codecov-shield]

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Issues][issues-shield]][issues-url]

[![MIT License][license-shield]][license-url]

<!-- PROJECT HEADER -->
<br />
<div align="center">
  <a href="https://github.com/totaldebug/luxmodbus">
    <h3 align="center">luxmodbus</h3>
  </a>

  <p align="center">
    Framing, register map, and discovery for the LuxPower inverter Modbus protocol.
  </p>
    <br />
    <a href="https://github.com/totaldebug/luxmodbus/issues/new?labels=type%2Fbug&template=bug_report.yml">Report Bug</a>
    ·
    <a href="https://github.com/totaldebug/luxmodbus/issues/new?labels=type%2Ffeature&template=feature_request.yml">Request Feature</a>
</div>

<!-- TABLE OF CONTENTS -->
<details>
  <summary>Table of Contents</summary>
  <ol>
    <li><a href="#about-the-project">About The Project</a></li>
    <li><a href="#getting-started">Getting Started</a></li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#the-frame">The Frame</a></li>
    <li><a href="#provenance">Provenance</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
  </ol>
</details>

## About The Project

`luxmodbus` is a small, dependency-free Python library for the **LuxPower
inverter Modbus protocol** — packet framing, the declarative register map, and
register discovery. It has **no Home Assistant dependency** and is the protocol
core consumed by the [Lumen](https://github.com/totaldebug/lumen) Home Assistant
integration. Because it imports nothing from Home Assistant, it can be tested
entirely offline against captured packet bytes.

### Status

Early. Implemented so far:

- `protocol.py` — frame encode/decode (LuxPower TCP envelope + inner Modbus RTU
  data frame), read/write request builders, and read-response unpacking. No I/O,
  no register-meaning knowledge.
- `registers.py` — declarative address → meaning map (the single source of
  truth) with a decode engine and a bounds-checked `encode_value` for writes.
- `discovery.py` — passive diff-and-log engine: compares observed registers
  against the known map and records the unknown with a rolling value history.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Getting Started

This project uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/totaldebug/luxmodbus.git
cd luxmodbus
uv sync
uv run nox -s tests
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Usage

```python
from luxmodbus import Frame, decode_inputs

frame = Frame.decode(raw_bytes)          # validates prefix + CRC
data = frame.data_frame()                # inner Modbus frame
# raw register values -> {key: scaled value}
values = decode_inputs({1: 2503, 4: 530, 5: (90 << 8) | 88})
# {"pv1_voltage": 250.3, "battery_voltage": 53.0, "soc": 88, "soh": 90}
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## The Frame

LuxPower wraps a modified Modbus RTU "data frame" inside a TCP envelope:

```
prefix(2)=A1 1A | protocol(u16 LE) | frame_length(u16 LE) | reserved(1)=01 |
tcp_function(1) | dongle_serial(10) | data_length(u16 LE) | data_frame(N) | crc16(u16 LE)
```

- `frame_length = total_len - 6`
- `data_length  = len(data_frame) + 2` (the trailing CRC)
- `crc16` is the standard Modbus CRC (poly `0xA001`, init `0xFFFF`) over the
  data frame, appended little-endian.

The inner data frame:

```
action(1) | device_function(1) | inverter_serial(10) | register(u16 LE) | value
```

See [`docs/capturing-packets.md`](docs/capturing-packets.md) for how to capture
real packets and turn them into test fixtures.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Provenance

Clean-room: the protocol *facts* (field layout, CRC algorithm, function codes,
register meanings) are taken from the official Lux Power Modbus RTU
specification and validated against real packet bytes. No code is copied from
other implementations.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Contributing

Contributions are welcome. Please open an issue first to discuss changes, then
ensure `uv run nox -s tests` passes (style, types, docstring coverage, and the
test suite) before opening a PR.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## License

Distributed under the MIT License. See `LICENSE` for more information.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- MARKDOWN LINKS & IMAGES -->
[release-shield]: https://img.shields.io/github/v/release/totaldebug/luxmodbus?style=for-the-badge
[release-url]: https://github.com/totaldebug/luxmodbus/releases
[stars-shield]: https://img.shields.io/github/stars/totaldebug/luxmodbus.svg?style=for-the-badge
[stars-url]: https://github.com/totaldebug/luxmodbus/stargazers
[codecov-shield]: https://img.shields.io/codecov/c/github/totaldebug/luxmodbus?style=for-the-badge
[contributors-shield]: https://img.shields.io/github/contributors/totaldebug/luxmodbus.svg?style=for-the-badge
[contributors-url]: https://github.com/totaldebug/luxmodbus/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/totaldebug/luxmodbus.svg?style=for-the-badge
[forks-url]: https://github.com/totaldebug/luxmodbus/network/members
[issues-shield]: https://img.shields.io/github/issues/totaldebug/luxmodbus.svg?style=for-the-badge
[issues-url]: https://github.com/totaldebug/luxmodbus/issues
[license-shield]: https://img.shields.io/github/license/totaldebug/luxmodbus.svg?style=for-the-badge
[license-url]: https://github.com/totaldebug/luxmodbus/blob/main/LICENSE
