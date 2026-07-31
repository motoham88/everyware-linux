#!/usr/bin/env python3
"""
ghprobe.py -- passive/interactive probe for the Green Heron remote switch panel.

The pcap summary told us the *shape* of the protocol but not the bytes. This
connects to the real device and dumps every byte it sends, framed and timestamped,
so the remaining unknowns (encoding, terminator, opcodes) answer themselves.

Usage:
    ./ghprobe.py 192.0.2.10            # listen only -- no keepalive, no commands
    ./ghprobe.py 192.0.2.10 --keepalive 0x0a
    ./ghprobe.py 192.0.2.10 --keepalive 0x0a --interactive

In --interactive mode, type at the prompt and press enter to send:
    hello            -> sends the literal bytes  h e l l o
    \\x41\\x42\\r      -> escapes are honoured (\\xNN, \\r, \\n, \\t, \\0)
    .raw 0d 0a       -> sends raw hex bytes
    .quit

Everything sent and received is appended to ghprobe.log as well as printed.
"""

import argparse
import socket
import sys
import threading
import time

LOG = open("ghprobe.log", "a", buffering=1)


def emit(line):
    print(line)
    LOG.write(line + "\n")


def hexdump(data, indent="    "):
    out = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        # split the 16 into 8+8 for readability
        if len(chunk) > 8:
            hexpart = " ".join(f"{b:02x}" for b in chunk[:8]) + "  " + \
                      " ".join(f"{b:02x}" for b in chunk[8:])
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{indent}{off:04x}  {hexpart:<49}  |{asc}|")
    return "\n".join(out)


def describe(data):
    """Point out the things we actually care about, so they don't get lost in the dump."""
    notes = []
    if all(32 <= b < 127 or b in (9, 10, 13) for b in data):
        notes.append("all-printable ASCII")
    else:
        notes.append("contains non-printable bytes")
    for name, b in (("CR", 0x0d), ("LF", 0x0a), ("NUL", 0x00), ("ETX", 0x03), ("SEMI", 0x3b)):
        if data.endswith(bytes([b])):
            notes.append(f"ends with {name}")
    return ", ".join(notes)


def reader(sock, start):
    """One read() per TCP segment as it arrives -- deliberately NOT reassembled,
    so the frame boundaries the device actually uses stay visible."""
    prev = None
    while True:
        try:
            data = sock.recv(65535)
        except OSError as e:
            emit(f"\n[recv error: {e}]")
            return
        if not data:
            emit("\n[device closed the connection]")
            return
        now = time.monotonic() - start
        delta = "" if prev is None else f"  (+{(now - prev) * 1000:7.1f} ms)"
        prev = now
        emit(f"\n<<< t={now:8.3f}s  len={len(data)}{delta}   [{describe(data)}]")
        emit(hexdump(data))
        emit(f"    repr: {data!r}")


ESCAPES = {"r": b"\r", "n": b"\n", "t": b"\t", "0": b"\x00", "\\": b"\\"}


def parse_line(line):
    if line.startswith(".raw "):
        return bytes(int(tok, 16) for tok in line[5:].split())
    out = bytearray()
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            nxt = line[i + 1]
            if nxt == "x" and i + 3 < len(line) + 1:
                out.append(int(line[i + 2:i + 4], 16))
                i += 4
                continue
            if nxt in ESCAPES:
                out += ESCAPES[nxt]
                i += 2
                continue
        out.append(ord(c))
        i += 1
    return bytes(out)


def keepalive_loop(sock, payload, interval, start):
    while True:
        time.sleep(interval)
        try:
            sock.sendall(payload)
        except OSError:
            return
        emit(f"\n>>> t={time.monotonic() - start:8.3f}s  keepalive {payload!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("-p", "--port", type=int, default=10000)
    ap.add_argument("--keepalive", metavar="BYTE",
                    help="single byte to send every --interval seconds, e.g. 0x0a or '\\r'")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="keepalive period in seconds (capture showed 5.000)")
    ap.add_argument("--interactive", action="store_true",
                    help="read command lines from stdin and send them")
    args = ap.parse_args()

    start = time.monotonic()
    emit(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  connecting to {args.host}:{args.port} =====")
    sock = socket.create_connection((args.host, args.port), timeout=10)
    sock.settimeout(None)
    emit(f"[connected from {sock.getsockname()[0]}:{sock.getsockname()[1]}]")
    emit("[sending nothing -- the capture shows the device transmits first]")

    threading.Thread(target=reader, args=(sock, start), daemon=True).start()

    if args.keepalive:
        ka = parse_line(args.keepalive) if args.keepalive.startswith("\\") \
            else bytes([int(args.keepalive, 0)])
        if len(ka) != 1:
            sys.exit("--keepalive must be exactly one byte")
        emit(f"[keepalive {ka!r} every {args.interval}s]")
        threading.Thread(target=keepalive_loop,
                         args=(sock, ka, args.interval, start), daemon=True).start()

    if args.interactive:
        try:
            for line in sys.stdin:
                line = line.rstrip("\n")
                if line == ".quit":
                    break
                if not line:
                    continue
                payload = parse_line(line)
                sock.sendall(payload)
                emit(f"\n>>> t={time.monotonic() - start:8.3f}s  len={len(payload)}  {payload!r}")
                emit(hexdump(payload))
        except KeyboardInterrupt:
            pass
    else:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass

    sock.close()
    emit("[closed]")


if __name__ == "__main__":
    main()
