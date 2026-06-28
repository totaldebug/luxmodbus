# Capturing packets from a LuxPower dongle

This guide explains how to capture real Wi-Fi-dongle traffic and turn it into
`*.bin` fixtures for `tests/fixtures/`. Those fixtures are the **second tier** of
protocol ground-truth: `test_captured_frames_round_trip` decodes each one and
asserts it re-encodes to identical bytes, so a real packet proves the framing,
not just our own synthetic vectors.

It's also the raw material for register **discovery** — diffing the official
app's traffic against the known register map.

> **Read-only and safe.** Capturing traffic is passive. You are sniffing bytes,
> not writing to the inverter. Nothing here changes a single setting.

---

## 1. How the dongle talks

The LuxPower Wi-Fi dongle speaks a TCP protocol (frames begin `A1 1A`):

- **Locally**, the dongle listens on **TCP port 8000**. A client on your LAN
  (the official app in "local" mode, `lxp-bridge`, or Lumen in client mode)
  opens a connection to `dongle-ip:8000` and exchanges frames.
- **To the cloud**, the dongle also dials out to the LuxPower servers so the
  phone app works remotely.

> Confirm the port and the dongle's IP from your router's DHCP lease table. 8000
> is the default; some firmware/regions differ.

The capture method depends on where you can see that traffic. Pick the first one
that fits your setup.

---

## 2. Method A — capture on the host that talks to the dongle (easiest)

If you run `lxp-bridge`, the old `luxpower` integration, or Lumen in client mode
on a Linux box (incl. a Home Assistant OS host with the **Advanced SSH & Web
Terminal** add-on in protection-mode-off), capture there. All the local traffic
flows through that host's interface.

```bash
# Replace 192.168.1.50 with your dongle's IP. Writes a rotating-free single file.
sudo tcpdump -i any -s 0 -w lux.pcap 'host 192.168.1.50 and tcp port 8000'
```

Let it run for a few minutes (a few poll cycles), then `Ctrl-C`. Copy `lux.pcap`
to your workstation.

`-s 0` captures full payloads (no truncation); `-i any` covers whichever
interface the traffic uses.

---

## 3. Method B — capture at the router / switch

If nothing on a Linux host talks to the dongle, capture where all its traffic
passes:

- **OpenWrt / DD-WRT router:** SSH in and run the same `tcpdump` command above
  (install `tcpdump` via `opkg install tcpdump` if needed).
- **OPNsense / pfSense router:** see the OPNsense walkthrough below (pfSense is
  near-identical).
- **Managed switch:** configure a **port-mirror (SPAN)** of the dongle's port to
  a port where a laptop runs Wireshark.
- **No mirror capability:** temporarily put a cheap hub (not a switch) inline, or
  use `arpspoof`-based bridging only on a network you own.

Filter to the dongle: `host <dongle-ip> and tcp port 8000`.

> **A router only sees what it *routes*.** It captures traffic that crosses an L3
> boundary — the dongle's outbound connection to the LuxPower cloud always does,
> so that is always visible. But the local `app/lxp-bridge/Lumen ↔ dongle:8000`
> exchange is switched at L2 and **never reaches the router** unless the two ends
> sit on **different VLANs/subnets** the router moves between. For purely-local
> traffic on one subnet, use Method A or a switch SPAN instead.

### Capturing on OPNsense

OPNsense (FreeBSD-based, so plain `tcpdump` is available) can capture either via
the GUI or the shell. Two gotchas drive the settings below:

- **Capture on the dongle's LAN-side interface, not WAN.** On WAN the dongle's
  source address is NAT'd to your WAN IP, so a `host <dongle-ip>` filter matches
  nothing there. On the dongle's own interface its real IP is visible, including
  for its WAN-bound (cloud) traffic.
- **Do not restrict to port 8000** if you are after the app's traffic. The local
  listener is `:8000`, but the dongle's outbound connection to the cloud uses a
  *different* destination port — filter by host only and identify the
  conversation afterwards (`tshark -z conv,tcp`).

**GUI — Interfaces → Diagnostics → Packet Capture:**

| Field | Value |
|---|---|
| Interface | the LAN/VLAN the dongle is on |
| Address | the dongle's IP (e.g. `192.168.1.50`) |
| Protocol | TCP |
| Port | *leave blank* (don't restrict to 8000) |
| Byte count / length | `0` — full payloads (= `tcpdump -s 0`) |
| Count | raise well above the default so it doesn't stop early |

Start it, change **one setting at a time** in the app (note the time), stop, then
**Download** the `.pcap`.

**Shell (SSH in) — equivalent to the above:**

```bash
# Find the dongle's interface (igb0, em0, vtnet0, or a vlan like igb0_vlan20…).
ifconfig

# Capture all of the dongle's TCP, full payloads, host filter only.
tcpdump -ni igb0 -s 0 -w /tmp/lux.pcap host 192.168.1.50 and tcp
```

`Ctrl-C` when done, then copy `/tmp/lux.pcap` off the box (e.g. `scp`).

---

## 4. Method C — capture the official app (highest signal for discovery)

The phone app exercises provisioning, firmware, and diagnostic registers that
neither open-source project documents. To capture the app talking to the dongle
**locally**:

1. Put your phone and the dongle on the same LAN.
2. Use the app's **local / direct** connection mode so it talks to
   `dongle-ip:8000` rather than the cloud.
3. Capture with Method A or B while you change one setting at a time in the app.
4. Diff the app's requests against the known register set — a hold register that
   changes right after you flip a setting *is* that setting.

If the app insists on the cloud, you'll instead see the dongle's outbound
connection; that traffic is still `A1 1A` framed and decodes the same way.

---

## 5. Turn the capture into `.bin` fixtures

We need each LuxPower frame as its own file of raw bytes (starting with
`A1 1A`). Two steps: export the TCP payload stream, then split it on frame
boundaries.

### 5a. Export the TCP payload bytes with `tshark`

`tshark` (the Wireshark CLI) reassembles the TCP stream and prints the payload
as hex. Find the stream number, then dump it:

```bash
# List TCP streams so you can pick the dongle conversation.
tshark -r lux.pcap -q -z conv,tcp

# Dump one direction (or both) of stream 0 as a continuous hex string.
tshark -r lux.pcap -Y 'tcp.stream==0 && tcp.len>0' \
  -T fields -e data.data | tr -d '\n:' > lux_payloads.hex
```

`data.data` is the raw TCP payload in hex. Concatenated frames are fine — the
splitter below uses each frame's own length field to cut them apart.

### 5b. Split into individual frames

This splitter uses the exact field layout `luxmodbus` implements: the prefix is
`A1 1A` and the total frame length is `frame_length + 6`, where `frame_length` is
the little-endian u16 at bytes 4–5.

```python
# split_frames.py  —  python split_frames.py lux_payloads.hex tests/fixtures
import sys
from pathlib import Path

PREFIX = b"\xa1\x1a"

def split(stream: bytes):
  i = 0
  while i + 6 <= len(stream):
    if stream[i : i + 2] != PREFIX:
      i += 1  # resync to the next prefix
      continue
    frame_length = int.from_bytes(stream[i + 4 : i + 6], "little")
    total = frame_length + 6
    if i + total > len(stream):
      break  # partial frame at the end of the capture
    yield stream[i : i + total]
    i += total

def main(hex_path: str, out_dir: str):
  stream = bytes.fromhex(Path(hex_path).read_text().strip())
  out = Path(out_dir)
  out.mkdir(parents=True, exist_ok=True)
  for n, frame in enumerate(split(stream)):
    (out / f"frame_{n:03d}.bin").write_bytes(frame)
    print(f"frame_{n:03d}.bin  {len(frame)} bytes")

if __name__ == "__main__":
  main(sys.argv[1], sys.argv[2])
```

```bash
python split_frames.py lux_payloads.hex tests/fixtures
```

### 5c. Verify they round-trip

```bash
pytest -k captured_frames -v
```

Each `frame_*.bin` should now be exercised by `test_captured_frames_round_trip`.
If one fails to decode, that's a genuinely interesting packet — keep it and open
an issue; it may use a frame variant we don't model yet.

### 5d. Analyse a capture for unknown registers

To go straight from a capture to a known-vs-unknown report — without splitting
into fixtures — use `scripts/analyze_capture.py`. It decodes every frame, looks
each register up in the map (decoding flags, times and selects), lists anything
**UNKNOWN**, and highlights registers the app **wrote**:

```bash
# Straight from the tshark hex dump produced in 5a:
python scripts/analyze_capture.py --hex lux_payloads.hex

# Or directly from the pcap (shells out to tshark), or from split *.bin frames:
python scripts/analyze_capture.py --pcap lux.pcap
python scripts/analyze_capture.py --bin 'tests/fixtures/*.bin'
```

For the discovery loop, pass `--state` to remember values between runs: capture
once to seed it, change **one** setting in the app, capture again, and the script
prints the register that changed.

```bash
python scripts/analyze_capture.py --hex before.hex --state /tmp/lux_state.json
# ...flip one setting in the app, capture again...
python scripts/analyze_capture.py --hex after.hex  --state /tmp/lux_state.json
# -> CHANGED since last run: hold:64 50->80
```

A hold register that changes (or is written) right after you toggle a setting
*is* that setting — note it, and open an issue or add it to the register map.

---

## 6. Scrub before sharing

Captured frames contain your **dongle serial** (bytes 8–17) and **inverter
serial** (inside the data frame). They are not secret credentials, but they
identify your hardware. Before committing fixtures to a public repo or attaching
them to an issue, consider overwriting the serials with placeholder bytes — the
CRC is over the *data frame* only, so changing the dongle serial in the envelope
does not require recomputing anything, while changing the inverter serial does
(re-run the bytes through `luxmodbus.crc16`).

---

## 7. Quick reference

| What | Value |
|---|---|
| Local dongle port | TCP **8000** (default) |
| Frame prefix | `A1 1A` |
| Total frame length | `frame_length + 6` (`frame_length` = u16 LE at bytes 4–5) |
| Capture filter | `host <dongle-ip> and tcp port 8000` |
| Fixture location | `tests/fixtures/*.bin` |
