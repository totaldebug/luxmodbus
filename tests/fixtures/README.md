# Captured packet fixtures

Drop real LuxPower packets here as raw bytes in `*.bin` files (one complete
frame per file, starting with the `A1 1A` prefix). `test_captured_frames_round_trip`
decodes each and asserts it re-encodes to the identical bytes — the second
("real") tier of protocol ground-truth.

See [`docs/capturing-packets.md`](../../docs/capturing-packets.md) for the full
walkthrough — capturing with `tcpdump`/`tshark` and a splitter that cuts the TCP
stream into individual `.bin` frames.
