# Green Heron remote switch panel — protocol

Device: `192.0.2.10:10000` (TCP). Official software: **GH Everyware Client**
(Windows; runs under Wine here). There is no vendor documentation — everything below
was reverse-engineered from packet captures and from talking to the live device.

The protocol is **fully decoded in both directions** and verified end-to-end: this
client's `SET_SWITCH` bytes are byte-identical to the official client's, and the device
responds to both the same way.

## Framing

ASCII, line-oriented:

| | | |
|---|---|---|
| `US` | `0x1f` | between fields |
| `GS` | `0x1d` | between subfields within a field |
| `CRLF` | `0x0d 0x0a` | ends a record |

The device transmits immediately on connect — no client hello, no login, no banner.
Confirmed twice: the first capture's packet 1 is device→client at `Seq=1 Ack=1`, and
`ghprobe` receives the roster 80 ms after connecting having sent nothing.

**Record boundaries do not align with TCP segments.** Observed, not assumed: one
585-byte segment carried three whole `SWITCHADD` records; the recurring 109-byte
segment carries `SWITCHLOCKS` + `SWITCHUPDATE` + `SWITCHLOCKS`, starting mid-cycle.
Buffer every `recv()` and split on CRLF. `tests/test_protocol.py` splits a record at
every possible byte offset, including between the CR and the LF.

## Device → client

```
SWITCHADD    ␟1␟AS-84F-1␟Beam-10␝0␝0␝false␟…9 ports…
SWITCHUPDATE ␟AS-84F-1␟OFF␟0␟-27
SWITCHLOCKS  ␟AS-84F-1␟OFF␟OFF␟OFF␟OFF
```

Four switches, `AS-84F-1` … `AS-84F-4`, nine ports each, identical across switches:
`Beam-10`, `Beam-15`, `Beam-20`, `EFHW-40`, `EFHW-80`, `Dipole-6`,
`Vertical-NoFilters`, `Dummy Load`, `OFF`. Note `Dummy Load` contains a space — split
on `US`, never on whitespace.

On connect the device sends four `SWITCHADD` records, then streams
`SWITCHUPDATE` + `SWITCHLOCKS` per switch, round-robin, every ~0.5–3.4 s indefinitely.

**The roster does not arrive in sorted order** — the device announced
`AS-84F-1, AS-84F-3, AS-84F-2, AS-84F-4`. Anything indexing switches by position must
not use arrival order; `client._sort_key` sorts by the trailing number. This is not
cosmetic: see the lock slot mapping below.

## Client → device

```
SET_SWITCH␟AS-84F-4␟Beam-20\r\n       29 bytes
SET_SWITCH␟AS-84F-4␟Dummy Load\r\n    32 bytes
SET_SWITCH␟AS-84F-4␟OFF\r\n           25 bytes
```

Captured from GH Everyware Client and reproduced by this client. Those lengths are
exactly the 29 / 32 / 25 seen in the very first capture, where only packet sizes were
available — the verb is 10 characters, as the length arithmetic in
`protocol.verbs_consistent_with()` required.

Fire-and-forget: no ack, no correlation id. The device confirms by pushing a fresh
`SWITCHUPDATE` **~123 ms** later (command at t=134.352, confirmation at t=134.475),
consistent with the 120–210 ms turnarounds in the original capture. Never model relay
state locally from commands sent — the device is the authority and it reports quickly.

### Keepalive

A single **NUL (`0x00`) every 5.000 s**. Measured across 38 consecutive keepalives:
intervals 4.9987–5.0007 s. Matches the 4.99956 / 5.00087 / 4.99979 timing in the
original capture.

It appears to matter. When the host laptop slept, the device dropped the idle
connection; the official client — unaware — sat retransmitting into a half-open socket
for over ten minutes with a growing `Send-Q`, and its queued NUL keepalives coalesced
into 29-byte all-zero segments. That is why `Client` reconnects with backoff rather
than trusting `ESTABLISHED` to mean anything.

## SWITCHLOCKS — confirmed

Slot N carries the port currently selected by switch N (in sorted order), republished
to **every** switch so a UI can grey out antennas in use elsewhere.

Confirmed directly: after `SET_SWITCH␟AS-84F-4␟Beam-20`, all four switches reported

```
SWITCHLOCKS|AS-84F-1|OFF|OFF|OFF|Beam-20
SWITCHLOCKS|AS-84F-2|OFF|OFF|OFF|Beam-20
SWITCHLOCKS|AS-84F-3|OFF|OFF|OFF|Beam-20
SWITCHLOCKS|AS-84F-4|OFF|OFF|OFF|Beam-20
```

Slot 4 for switch 4 — which also confirms sorted order is the right mapping, since
arrival order (1, 3, 2, 4) would have put it in slot 2.

Locks propagate one switch per round-robin step, so the full set takes a few seconds
to converge. A UI showing lock state will briefly show it on some switches and not
others; this is the device's cadence, not a bug.

## Still unknown

Parsed into named fields and carried through, but nothing branches on them and the TUI
shows them without an interpretive label:

- `SWITCHUPDATE` field 4 — `-27` on switches 1–2, `-28` on 3–4, stable across every
  sample including while relays were switching. Could be signal, temperature, or a raw
  ADC count. Not enough evidence to pick one, so it is displayed raw and unlabelled.
- `SWITCHADD` leading `1`, and the per-port `0␝0␝false` subfields — identical on all
  four switches and unchanged by any operation performed so far.
- Whether the device supports records beyond the three seen. Unknown verbs parse to
  `Unknown` and appear in the TUI's raw pane (`r`) rather than raising.
- Whether a UDP discovery/announce path exists alongside the TCP port — never checked.

## A note on the failed verb search

Before the capture, `tools/find_verb.py` sent 20 candidate verbs of the two lengths the
arithmetic permitted. None worked — every candidate was built from letters only, and
the real verb contains an underscore. The probe was also confounded: it never sent the
NUL prefix the official client sends before its first command, so a wrong verb and an
un-enabled connection would have looked identical.

Worth remembering: the length arithmetic was right and narrowed the verb to exactly 10
characters, but guessing the *string* was never going to work. Ten minutes of tcpdump
answered the verb, the keepalive byte, and the lock semantics at once.

## Tools

| | |
|---|---|
| `ghprobe.py` | connect and hexdump everything, with timestamps and inter-frame gaps |
| `tools/extract_commands.py` | pull client→device payloads from a pcap and validate the model |
| `tools/find_verb.py` | the probe that did not work; kept for the record |
