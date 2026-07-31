"""
Connection and state tracking for the Green Heron switch panel.

Threading model: one reader thread owns the socket's receive side and is the only
writer of panel state; one keepalive thread writes a byte every 5 s. Callers read
state through `snapshot()`, which returns an immutable copy under a lock.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from greenheron import protocol as p

#: CONFIRMED from a capture of GH Everyware Client operating AS-84F-4:
#:
#:     SET_SWITCH\x1fAS-84F-4\x1fBeam-20\r\n       29 bytes
#:     SET_SWITCH\x1fAS-84F-4\x1fDummy Load\r\n    32 bytes
#:     SET_SWITCH\x1fAS-84F-4\x1fOFF\r\n           25 bytes
#:
#: Those are exactly the 29/32/25 lengths seen in the original capture, and the
#: verb is 10 characters as the length arithmetic required.
SELECT: Optional[p.CommandFormat] = p.CommandFormat("SET_SWITCH")


def _sort_key(name: str) -> tuple:
    """Sort AS-84F-2 before AS-84F-10, and anything unnumbered last."""
    head, _, tail = name.rpartition("-")
    if head and tail.isdigit():
        return (0, head, int(tail))
    return (1, name, 0)


@dataclass(frozen=True)
class SwitchState:
    name: str
    ports: tuple[str, ...] = ()
    selected: str = ""
    locks: tuple[str, ...] = ()
    wireless_signal: str = ""
    last_update: float = 0.0


@dataclass
class Panel:
    """Everything the device has told us. Never inferred from commands sent."""
    switches: dict[str, SwitchState] = field(default_factory=dict)
    #: Switch names in the order the device announced them via SWITCHADD.
    #: This is NOT display order -- it is the index basis for lock slots.
    announced: list[str] = field(default_factory=list)
    connected: bool = False
    stale: bool = False
    last_error: str = ""
    connected_since: float = 0.0

    @property
    def order(self) -> list[str]:
        """Switch names in a stable, predictable order.

        Display order only. The device announces its roster as AS-84F-1,
        AS-84F-3, AS-84F-2, AS-84F-4, which would make TUI columns jump around;
        sorting by the trailing number keeps them put. Lock slots are indexed by
        `announced`, not by this -- see `locks_by_switch`.
        """
        return sorted(self.switches, key=_sort_key)

    def locks_by_switch(self, name: str) -> dict[str, str]:
        """Which *other* switch, if any, currently holds each port.

        Slot N holds the port selected by the Nth switch **in announcement
        order** -- the order the device sent its SWITCHADD records, which on this
        hardware is 1, 3, 2, 4 rather than sorted.

        Verified with a discriminating case: selecting Beam-15 on AS-84F-3 put it
        in slot 1, and AS-84F-3 is announced second. Sorted order would predict
        slot 2. An earlier test using AS-84F-4 appeared to confirm sorted order
        but proved nothing -- AS-84F-4 is index 3 under both orderings.
        """
        me = self.switches.get(name)
        if not me or not me.locks:
            return {}
        order = self.announced
        held: dict[str, str] = {}
        for i, port in enumerate(me.locks):
            if i >= len(order) or order[i] == name:
                continue
            if port and port != "OFF":
                held[port] = order[i]
        return held


class Client:
    """Connects, keeps the connection up, and tracks panel state."""

    def __init__(
        self,
        host: str,
        port: int = p.DEFAULT_PORT,
        on_change: Optional[Callable[[Panel], None]] = None,
        on_record: Optional[Callable[[p.Record], None]] = None,
        dry_run: bool = False,
        keepalive: bool = True,
    ):
        self.host = host
        self.port = port
        self.on_change = on_change
        self.on_record = on_record
        self.dry_run = dry_run
        self.keepalive = keepalive

        self._lock = threading.Lock()
        self._panel = Panel()
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.sent_log: list[bytes] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for target in (self._run, self._keepalive_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sock, self._sock = self._sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def snapshot(self) -> Panel:
        with self._lock:
            return replace(
                self._panel,
                switches=dict(self._panel.switches),
                announced=list(self._panel.announced),
            )

    # -- sending -----------------------------------------------------------

    def select(self, switch: str, port: str, allow_unconfirmed: bool = False) -> bytes:
        """Ask the device to put `switch` on `port`.

        Returns the bytes sent. Does not update local state: the device is the
        only authority on what the relays are doing, and it republishes within
        ~200 ms.
        """
        # The guard exists to stop guessed commands reaching real hardware, so
        # it does not apply when nothing is transmitted.
        if SELECT is None and not allow_unconfirmed and not self.dry_run:
            raise RuntimeError(
                "The select command is not known yet. Capture it with tcpdump and "
                "tools/extract_commands.py, then set greenheron.client.SELECT."
            )
        fmt = SELECT or p.CommandFormat("SWITCHPORT")
        wire = fmt.encode(switch, port)
        self.sent_log.append(wire)
        if not self.dry_run:
            self._send(wire)
        return wire

    def _send(self, data: bytes) -> None:
        with self._lock:
            sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        sock.sendall(data)

    # -- threads -----------------------------------------------------------

    def _keepalive_loop(self) -> None:
        """Mirrors the official client: a single NUL every 5.000 s.

        The byte is confirmed from a capture of GH Everyware Client, so this is
        on by default. It matters: when the host laptop slept, the device dropped
        the idle connection, and the official client -- unaware -- retransmitted
        its queued keepalives for ten minutes into a half-open socket. Hence the
        reconnect path below rather than trusting ESTABLISHED to mean anything.
        """
        while not self._stop.wait(p.KEEPALIVE_INTERVAL):
            if not self.keepalive or self.dry_run:
                continue
            try:
                self._send(p.KEEPALIVE_BYTE)
            except (OSError, ConnectionError):
                pass  # the reader loop owns reconnection

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=10)
                sock.settimeout(None)
                with self._lock:
                    self._sock = sock
                    self._panel = replace(
                        self._panel, connected=True, stale=False,
                        last_error="", connected_since=time.time(),
                    )
                self._notify()
                backoff = 1.0
                self._read_forever(sock)
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                with self._lock:
                    self._panel = replace(self._panel, last_error=str(exc))

            # Keep the last known state visible but flagged, rather than blanking
            # the panel on every blip.
            with self._lock:
                self._sock = None
                self._panel = replace(self._panel, connected=False, stale=True)
            self._notify()

            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    def _read_forever(self, sock: socket.socket) -> None:
        buf = b""
        while not self._stop.is_set():
            data = sock.recv(65535)
            if not data:
                raise ConnectionError("device closed the connection")
            # Records do not align with segments -- always split, never assume.
            records, buf = p.split_records(buf + data)
            if not records:
                continue
            for raw in records:
                rec = p.parse(raw)
                self._apply(rec)
                if self.on_record:
                    self.on_record(rec)
            self._notify()

    # -- state -------------------------------------------------------------

    def _apply(self, rec: p.Record) -> None:
        now = time.time()
        with self._lock:
            switches = dict(self._panel.switches)

            if isinstance(rec, p.SwitchAdd):
                prev = switches.get(rec.switch)
                switches[rec.switch] = replace(
                    prev or SwitchState(rec.switch),
                    ports=rec.port_names, last_update=now,
                )
                # First-seen SWITCHADD order fixes the lock slot mapping. Append
                # only, so a reconnect replaying the roster cannot reorder it.
                if rec.switch not in self._panel.announced:
                    self._panel.announced.append(rec.switch)
            elif isinstance(rec, p.SwitchUpdate):
                prev = switches.get(rec.switch) or SwitchState(rec.switch)
                switches[rec.switch] = replace(
                    prev, selected=rec.selected,
                    wireless_signal=rec.wireless_signal, last_update=now,
                )
            elif isinstance(rec, p.SwitchLocks):
                prev = switches.get(rec.switch) or SwitchState(rec.switch)
                switches[rec.switch] = replace(prev, locks=rec.locks, last_update=now)
            else:
                return

            self._panel = replace(self._panel, switches=switches)

    def _notify(self) -> None:
        if self.on_change:
            self.on_change(self.snapshot())
