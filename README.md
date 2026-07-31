# GHClient

A Linux client for the Green Heron remote antenna switch panel — a curses TUI for a
4-switch × 9-antenna matrix, speaking the device's undocumented TCP protocol on port
10000.

Three front ends over one client library — a terminal panel, a GTK4 desktop app,
and an MQTT bridge with Home Assistant discovery.

```
./gh-panel 192.0.2.10          # curses TUI; your switch's address
./gh-gui   192.0.2.10          # GTK4 window
./gh-mqtt  192.0.2.10 --broker 192.0.2.20
export GH_SWITCH_HOST=...      # ...or set this and pass no address
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

A few `SWITCHADD` sub-fields still have no established meaning; they are parsed and
carried but nothing branches on them. See NOTES.md for the full protocol.

## Home Assistant

```
pip install -r requirements-mqtt.txt
./gh-mqtt 192.0.2.10 --broker 192.0.2.20 --username ha --password ...
```

Each switch is announced as a **`select`** entity whose options are the antenna
names the device advertises, so all four appear under one HA device as four
dropdowns. That is the entity type that matches the hardware: a switch is on
exactly one of nine ports, which is a choice, not a set of toggles. Nothing to add
to `configuration.yaml` — discovery configs are published retained, so Home
Assistant repopulates on its own after a restart.

| topic | |
|---|---|
| `greenheron/availability` | `online` / `offline` — also the last will |
| `greenheron/as_84f_1/state` | selected port, retained |
| `greenheron/as_84f_1/set` | write a port name here to select it |
| `greenheron/as_84f_1/attributes` | JSON: locks, holder, ports, wireless signal |

Entities go unavailable when the bridge loses either the broker or the switch, so
a stale dropdown never looks live. Commands are published with `optimistic: false`
— HA waits for the device to confirm rather than assuming the relay moved.

Credentials are read from `$GH_MQTT_USERNAME` / `$GH_MQTT_PASSWORD` as well as
flags, which keeps them out of your shell history and out of `ps`.

To run it as a service, see `packaging/gh-mqtt.service`.

## Layout

| | |
|---|---|
| `greenheron/protocol.py` | framing and record parsing — pure, no I/O |
| `greenheron/client.py` | socket, state tracking, reconnect, keepalive |
| `greenheron/tui.py` | curses panel |
| `greenheron/gui.py` | GTK4 panel |
| `greenheron/mqtt_bridge.py` | MQTT + Home Assistant discovery |
| `packaging/` | systemd user unit and environment file |
| `ghprobe.py` | hexdump whatever the device sends — the debugging oracle |
| `tools/` | pcap extraction and protocol experiments |
| `NOTES.md` | the protocol, how each claim was established, and open questions |

## Tests

```
python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/
```

`--system-site-packages` is what lets the venv see PyGObject, which is a distro
package rather than a wheel. Without it the GTK tests skip and the rest still run.

Protocol fixtures are verbatim bytes captured off the device, not hand-written.
Connection tests run against a local stand-in server, including records delivered one
byte at a time and mid-session disconnects. No hardware needed.

Addresses in the docs use `192.0.2.10` — the RFC 5737 documentation range, not a real
host.

## License

MIT. Not affiliated with or endorsed by Green Heron Engineering; the protocol was
determined by observing traffic between their client and their device for
interoperability.
