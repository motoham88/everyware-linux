#!/usr/bin/env python3
"""
OBSOLETE. The verb is SET_SWITCH -- captured from GH Everyware Client, confirmed
byte-for-byte. This script is kept only as a record of what was tried.

Do not run it. It sends 20 speculative writes to real hardware and can no longer
learn anything. Every candidate below is also wrong: the search space was built
from letters only, and the real verb contains an underscore.

Historical description follows.

Find the select-command verb by asking the device.

The length arithmetic in greenheron.protocol narrows the command to exactly two
shapes -- a 10-character verb with no group field, or an 8-character verb with
one -- so this is a short constrained list, not a fishing expedition.

Method: the device republishes state continuously. Send a candidate, then watch
whether the target switch's SWITCHUPDATE changes to the requested port within a
couple of seconds. A wrong verb should change nothing.

    ./tools/find_verb.py 192.0.2.10 --switch AS-84F-1 --port Beam-20

SAFETY: a correct guess physically throws an RF relay. Only run this with nothing
connected to the switch ports and no transmitter keyed. --dry-run prints the
candidates without sending anything.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])

from greenheron import protocol as p  # noqa: E402

# Verbs of the only two lengths the observed 29/29/32/25 command sizes permit.
# The device's own vocabulary is SWITCHADD / SWITCHUPDATE / SWITCHLOCKS, so
# SWITCH+noun is weighted first.
CANDIDATES_10 = [
    "SWITCHPORT", "SWITCHITEM", "SWITCHPICK", "SWITCHSEL1",
    "SELECTPORT", "PORTSELECT", "SETANTENNA", "ANTENNASET",
    "SWITCHNAME", "SETSWITCH1", "CHANGEPORT", "SWITCHBANK",
]
CANDIDATES_8 = [
    "SWITCHTO", "SETPORTS", "PORTSETS", "SELPORTS",
    "SWITCHIT", "ANTSWTCH", "SETSWTCH", "SWCHPORT",
]


def candidates() -> list[p.CommandFormat]:
    out = [p.CommandFormat(v) for v in CANDIDATES_10]
    out += [p.CommandFormat(v, include_group=True) for v in CANDIDATES_8]
    return out


class Watcher:
    """Tracks the selected port per switch from the device's own stream."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.selected: dict[str, str] = {}
        self.other: list[str] = []

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                return
            if not data:
                raise ConnectionError("device closed the connection")
            records, self.buf = p.split_records(self.buf + data)
            for raw in records:
                rec = p.parse(raw)
                if isinstance(rec, p.SwitchUpdate):
                    self.selected[rec.switch] = rec.selected
                elif isinstance(rec, p.Unknown):
                    # An error reply to a bad verb would show up here.
                    self.other.append(rec.raw.decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("-p", "--port", type=int, default=p.DEFAULT_PORT)
    ap.add_argument("--switch", default="AS-84F-1")
    ap.add_argument("--target", default="Beam-20", help="port to try selecting")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="seconds to watch after each candidate")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent, send nothing")
    ap.add_argument("--yes-i-know-this-is-obsolete", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not args.dry_run and not args.yes_i_know_this_is_obsolete:
        print(__doc__.strip().split("Historical description")[0])
        print("Refusing to run. Use --dry-run to see the candidates.")
        return 2

    cands = candidates()
    if args.dry_run:
        for fmt in cands:
            wire = fmt.encode(args.switch, args.target)
            print(f"{len(wire):3d}  {wire!r}")
        return 0

    sock = socket.create_connection((args.host, args.port), timeout=10)
    w = Watcher(sock)

    print(f"[baseline] listening for the state of {args.switch} ...")
    w.pump(4.0)
    baseline = w.selected.get(args.switch)
    if baseline is None:
        print(f"never saw a SWITCHUPDATE for {args.switch}; known: {sorted(w.selected)}")
        return 2
    print(f"[baseline] {args.switch} = {baseline!r}")
    if baseline == args.target:
        print(f"already on {args.target}; pick a different --target")
        return 2

    found = None
    for fmt in cands:
        wire = fmt.encode(args.switch, args.target)
        print(f"  -> {fmt.verb:<12} ({len(wire):>2}B) {wire!r}", flush=True)
        sock.sendall(wire)
        w.pump(args.settle)
        now = w.selected.get(args.switch)
        if now != baseline:
            print(f"\n*** {args.switch} changed {baseline!r} -> {now!r} after {fmt.verb}")
            found = fmt
            break

    if w.other:
        print("\nunrecognised records seen (possible error replies):")
        for line in w.other[-10:]:
            print(f"    {line!r}")

    if not found:
        print(f"\nNo candidate moved {args.switch}. The verb is outside this list, or")
        print("the field model is wrong. Fall back to tcpdump + tools/extract_commands.py.")
        sock.close()
        return 1

    print(f"\nRestoring {args.switch} to {baseline!r} ...")
    sock.sendall(found.encode(args.switch, baseline))
    w.pump(args.settle)
    print(f"{args.switch} = {w.selected.get(args.switch)!r}")

    print("\n" + "=" * 60)
    print(f"verb           {found.verb!r}")
    print(f"include_group  {found.include_group}")
    print(f"wire format    {found.encode('<switch>', '<port>')!r}")
    print("=" * 60)
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
