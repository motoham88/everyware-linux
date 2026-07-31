"""
Wire protocol for the Green Heron remote switch panel (TCP port 10000).

Reverse-engineered from a live device; there is no vendor documentation. See
NOTES.md for how each claim here was established.

The device speaks a line-oriented ASCII protocol:

    record    := VERB US field (US field)* CRLF
    field     := text | subfield (GS subfield)*

    US  = 0x1f  (ASCII unit separator)   -- between fields
    GS  = 0x1d  (ASCII group separator)  -- between subfields within one field
    CRLF= 0x0d 0x0a                      -- ends a record

Everything in this module is pure: no sockets, no threads. That is deliberate --
it lets the parser be tested against bytes captured off the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

US = b"\x1f"
GS = b"\x1d"
CRLF = b"\r\n"

DEFAULT_PORT = 10000

# CONFIRMED: the official client (GH Everyware Client) sends a single NUL every
# 5.000 s. Captured at t=4.944 / 9.945 / 14.945 on a fresh connection -- intervals
# of 5.0005 and 4.9996 s, matching the 4.99956 / 5.00087 / 4.99979 seen in the
# original capture.
KEEPALIVE_INTERVAL = 5.0
KEEPALIVE_BYTE = b"\x00"


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

def split_records(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split a receive buffer into complete records, returning the partial tail.

    Record boundaries do NOT align with TCP segments. This is observed, not
    assumed: one 585-byte segment from the device carried three whole SWITCHADD
    records, while the recurring 109-byte segment carries SWITCHLOCKS +
    SWITCHUPDATE + SWITCHLOCKS. Feed every recv() through here and keep the
    remainder for next time.
    """
    parts = buf.split(CRLF)
    remainder = parts.pop()  # after a trailing CRLF this is b"", which is correct
    return [p for p in parts if p], remainder


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Port:
    """One selectable antenna position, as advertised in SWITCHADD.

    Only `name` has known meaning. Every port on every switch reported
    `0 / 0 / false`, so the capture carries no information about what the other
    three subfields do -- they are carried through verbatim and never branched on.
    """
    name: str
    unknown_a: str = "0"
    unknown_b: str = "0"
    unknown_flag: str = "false"


@dataclass(frozen=True)
class SwitchAdd:
    """Roster for one switch. Sent four times immediately on connect."""
    switch: str
    ports: tuple[Port, ...]
    unknown_group: str = "1"  # leading field, always "1" in the capture
    raw: bytes = b""

    @property
    def port_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.ports)


@dataclass(frozen=True)
class SwitchUpdate:
    """Current selection for one switch.

    `wireless_signal` is the Green Heron wireless link signal -- -27 or -28
    depending on the switch, and unrelated to antenna selection. It is parsed and
    carried, but no front end displays it and no unit is asserted for it.
    """
    switch: str
    selected: str
    unknown_c: str = "0"
    wireless_signal: str = ""
    raw: bytes = b""


@dataclass(frozen=True)
class SwitchLocks:
    """Four lock slots for one switch.

    Slot N carries the port selected by the Nth switch **in announcement order**,
    republished to every switch so a UI can grey out antennas in use elsewhere.
    Confirmed on hardware; see NOTES.md, including why an earlier test appeared
    to confirm sorted order and did not. `Panel.locks_by_switch()` is the only
    place that acts on this.
    """
    switch: str
    locks: tuple[str, ...] = ()
    raw: bytes = b""


@dataclass(frozen=True)
class Unknown:
    """A record whose verb we have never seen.

    The sample was 32 seconds of an idle device. It would be optimistic to assume
    that is the whole vocabulary, so unrecognised verbs are surfaced rather than
    raising -- the TUI's raw pane shows them.
    """
    verb: str
    fields: tuple[str, ...] = ()
    raw: bytes = b""


Record = Union[SwitchAdd, SwitchUpdate, SwitchLocks, Unknown]


def parse(record: bytes) -> Record:
    """Parse one complete record (no trailing CRLF)."""
    parts = record.split(US)
    verb = _decode(parts[0])
    rest = parts[1:]

    if verb == "SWITCHADD" and len(rest) >= 2:
        ports = []
        for raw_port in rest[2:]:
            sub = [_decode(s) for s in raw_port.split(GS)]
            sub += [""] * (4 - len(sub))
            ports.append(Port(sub[0], sub[1], sub[2], sub[3]))
        return SwitchAdd(
            switch=_decode(rest[1]),
            ports=tuple(ports),
            unknown_group=_decode(rest[0]),
            raw=record,
        )

    if verb == "SWITCHUPDATE" and len(rest) >= 2:
        f = [_decode(x) for x in rest] + [""] * 4
        return SwitchUpdate(
            switch=f[0], selected=f[1], unknown_c=f[2], wireless_signal=f[3], raw=record
        )

    if verb == "SWITCHLOCKS" and len(rest) >= 1:
        return SwitchLocks(
            switch=_decode(rest[0]),
            locks=tuple(_decode(x) for x in rest[1:]),
            raw=record,
        )

    return Unknown(verb=verb, fields=tuple(_decode(x) for x in rest), raw=record)


# --------------------------------------------------------------------------
# Commands (client -> device)
# --------------------------------------------------------------------------
#
# NOT yet confirmed against the wire. The original capture showed four operator
# commands of 29, 29, 32 and 25 bytes but the payloads were never captured.
#
# Two field models survive that evidence, both assuming the same US/CRLF framing
# the device uses in the other direction:
#
#   A:  VERB US switch US port CRLF        length = len(VERB) + 12 + len(port)
#   B:  VERB US "1" US switch US port CRLF length = len(VERB) + 14 + len(port)
#
# Switch names are 8 characters ("AS-84F-1"). Solving model A across all four
# observed lengths admits exactly one verb length -- 10 -- giving port name
# lengths of 7, 7, 10 and 3. Those correspond to a 7-character antenna, another,
# "Dummy Load" and "OFF": a coherent operator session, which is good evidence the
# framing model is right even though the verb itself is not yet known. Model B
# fits equally with an 8-character verb.

@dataclass(frozen=True)
class CommandFormat:
    """A candidate encoding for the select command."""
    verb: str
    include_group: bool = False
    group: str = "1"

    @property
    def overhead(self) -> int:
        return len(self.verb) + (14 if self.include_group else 12)

    def encode(self, switch: str, port: str) -> bytes:
        fields = [self.verb]
        if self.include_group:
            fields.append(self.group)
        fields += [switch, port]
        return US.join(f.encode() for f in fields) + CRLF

    def predicted_length(self, switch: str, port: str) -> int:
        # only exact for 8-character switch names, which is what this device uses
        return self.overhead + len(port) + (len(switch) - 8)


#: Observed client->device payload lengths from the original Wireshark capture,
#: in order. Used by `verbs_consistent_with` to constrain the search.
OBSERVED_COMMAND_LENGTHS = (29, 29, 32, 25)

#: Port names this device advertises, longest first for length matching.
KNOWN_PORTS = (
    "Vertical-NoFilters",
    "Dummy Load",
    "Dipole-6",
    "Beam-10",
    "Beam-15",
    "Beam-20",
    "EFHW-40",
    "EFHW-80",
    "OFF",
)


def verbs_consistent_with(
    lengths: tuple[int, ...] = OBSERVED_COMMAND_LENGTHS,
    ports: tuple[str, ...] = KNOWN_PORTS,
    switch_len: int = 8,
) -> dict[bool, list[int]]:
    """Which verb lengths can explain every observed command length?

    Returns {include_group: [viable verb lengths]}. This is the validator for the
    field model, not a way to guess the verb: once real command bytes exist, they
    must agree with one of these, and if they agree with neither then something
    in the field model above is wrong and needs fixing before anything is built
    on top of it.
    """
    port_lens = {len(p) for p in ports}
    out: dict[bool, list[int]] = {}
    for include_group in (False, True):
        overhead = (14 if include_group else 12) + (switch_len - 8)
        viable = [
            v
            for v in range(1, 25)
            if all((n - v - overhead) in port_lens for n in lengths)
        ]
        out[include_group] = viable
    return out
