# GHClient

A Linux client for the Green Heron remote antenna switch panel — a curses TUI for a
4-switch × 9-antenna matrix, speaking the device's undocumented TCP protocol on port
10000.

```
./gh-panel 192.0.2.10          # your switch's address
export GH_SWITCH_HOST=...      # ...or set this and just run ./gh-panel
```

```
Green Heron switch panel — 192.0.2.10:10000              connected 0m08s

Antenna              AS-84F-1  AS-84F-2  AS-84F-3  AS-84F-4
Beam-10                 ·         ·         ·         ·
Beam-15                 ·         ·         ·         ·
Beam-20                 ·         ·         ·         ·
EFHW-40                 ·         ·         ·         ·
...
OFF                    ● ON      ● ON      ● ON      ● ON
```

`↑↓` port · `←→` switch · `⏎` select · `o` OFF · `r` raw protocol · `q` quit

`--dry-run` exercises the panel without transmitting anything. `--no-keepalive` omits
the 5 s NUL the official client sends.

## Status

Working. The protocol is decoded in both directions and verified against the hardware:
this client's `SET_SWITCH` bytes are byte-identical to those of the official GH
Everyware Client, and the device responds to both identically.

- Live state for all four switches, refreshed every ~0.5–3.4 s
- Antenna selection, confirmed by the device in ~123 ms
- Lock display — antennas held by another switch are marked `◦ N`
- Reconnect with backoff; last known state stays visible, flagged stale

Two `SWITCHADD`/`SWITCHUPDATE` fields have no known meaning and are shown raw rather
than guessed at. See NOTES.md for the full protocol and the remaining unknowns.

## Layout

| | |
|---|---|
| `greenheron/protocol.py` | framing and record parsing — pure, no I/O |
| `greenheron/client.py` | socket, state tracking, reconnect, keepalive |
| `greenheron/tui.py` | curses panel |
| `ghprobe.py` | hexdump whatever the device sends — the debugging oracle |
| `tools/` | pcap extraction and protocol experiments |
| `NOTES.md` | the protocol, how each claim was established, and open questions |

## Tests

```
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/
```

Protocol fixtures are verbatim bytes captured off the device, not hand-written.
Connection tests run against a local stand-in server, including records delivered one
byte at a time and mid-session disconnects. No hardware needed.

Addresses in the docs use `192.0.2.10` — the RFC 5737 documentation range, not a real
host.

## License

MIT. Not affiliated with or endorsed by Green Heron Engineering; the protocol was
determined by observing traffic between their client and their device for
interoperability.
