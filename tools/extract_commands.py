#!/usr/bin/env python3
"""
Pull client->device payloads out of a capture and check them against the field model.

The receive direction is already solved -- this only cares about what the official
client sends, which is the one remaining unknown.

    sudo tcpdump -i any -s0 -w gh-cmd.pcap 'host 192.0.2.10 and tcp port 10000'
    # ...operate the official GH client, noting each switch/antenna you touch...
    ./tools/extract_commands.py gh-cmd.pcap --device 192.0.2.10
"""

from __future__ import annotations

import argparse
import binascii
import subprocess
import sys
from collections import Counter

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])

from greenheron import protocol as p  # noqa: E402


def payloads(path: str, device: str, port: int):
    """(time, payload) for every client->device segment carrying data."""
    out = subprocess.run(
        ["tshark", "-r", path,
         "-Y", f"ip.dst=={device} && tcp.dstport=={port} && tcp.len>0",
         "-T", "fields", "-e", "frame.time_relative", "-e", "tcp.payload"],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        if "\t" not in line:
            continue
        t, hexdata = line.split("\t", 1)
        hexdata = hexdata.replace(":", "").strip()
        if hexdata:
            yield float(t), binascii.unhexlify(hexdata)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("--device", required=True, help="the switch's address")
    ap.add_argument("--port", type=int, default=p.DEFAULT_PORT)
    args = ap.parse_args()

    items = list(payloads(args.pcap, args.device, args.port))
    if not items:
        print("No client->device payloads found. Wrong --device, or the capture "
              "only caught the receive direction.")
        return 1

    singles, commands = [], []
    print(f"{'time':>9}  {'len':>4}  payload")
    print("-" * 72)
    for t, data in items:
        print(f"{t:9.3f}  {len(data):4d}  {data!r}")
        (singles if len(data) == 1 else commands).append((t, data))

    if singles:
        seen = Counter(d for _, d in singles)
        gaps = [round(b - a, 4) for (a, _), (b, _) in zip(singles, singles[1:])]
        print(f"\nKeepalive: {len(singles)} single-byte writes, values {dict(seen)}")
        if gaps:
            print(f"  intervals: {gaps}")
        if len(seen) == 1:
            byte = next(iter(seen))
            print(f"  => set protocol.KEEPALIVE_BYTE = {byte!r}")

    if not commands:
        print("\nNo multi-byte commands captured -- operate the switch while capturing.")
        return 1

    print("\nCommands:")
    for t, data in commands:
        fields = data.rstrip(p.CRLF).split(p.US)
        verb = fields[0].decode("utf-8", "replace")
        rest = [f.decode("utf-8", "replace") for f in fields[1:]]
        print(f"{t:9.3f}  {len(data):4d}  verb={verb!r} fields={rest}")

    # Does the model in protocol.py actually explain these?
    verbs = {d.split(p.US)[0] for _, d in commands}
    print("\n" + "=" * 72)
    if len(verbs) == 1:
        verb = next(iter(verbs)).decode("utf-8", "replace")
        n_fields = len(commands[0][1].rstrip(p.CRLF).split(p.US))
        include_group = n_fields == 4
        fmt = p.CommandFormat(verb, include_group=include_group)
        ok = all(
            len(d) == fmt.predicted_length(*(
                f.decode() for f in d.rstrip(p.CRLF).split(p.US)[-2:]))
            for _, d in commands
        )
        print(f"verb           {verb!r}  ({len(verb)} chars)")
        print(f"include_group  {include_group}")
        print(f"length model   {'CONFIRMED' if ok else 'DOES NOT FIT -- field model is wrong'}")
        print(f"\n  greenheron/client.py:  SELECT = CommandFormat({verb!r}"
              f"{', include_group=True' if include_group else ''})")
    else:
        print(f"Several command verbs: {sorted(v.decode() for v in verbs)}")
        print("More than one command type -- map each to the control you operated.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
