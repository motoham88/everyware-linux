"""Connection behaviour against a local stand-in for the device.

Covers the things that only show up over a real socket: records split across
segments, surviving a dropped connection, and the keepalive.
"""

import socket
import threading
import time

import pytest

from greenheron import client as gh_client
from greenheron import protocol as p

ROSTER = (
    b"SWITCHADD\x1f1\x1fAS-84F-1\x1fBeam-20\x1d0\x1d0\x1dfalse"
    b"\x1fOFF\x1d0\x1d0\x1dfalse\r\n"
)
UPDATE = b"SWITCHUPDATE\x1fAS-84F-1\x1fBeam-20\x1f0\x1f-27\r\n"


class FakeDevice:
    """Accepts one connection at a time and replays whatever it is told to."""

    def __init__(self, script=(ROSTER, UPDATE), chunk=None):
        self.script = script
        self.chunk = chunk
        self.received = bytearray()
        self.connections = 0
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._drop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self._srv.settimeout(0.2)
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            self._drop.clear()
            try:
                payload = b"".join(self.script)
                if self.chunk:
                    for i in range(0, len(payload), self.chunk):
                        conn.sendall(payload[i:i + self.chunk])
                        time.sleep(0.005)
                else:
                    conn.sendall(payload)
                conn.settimeout(0.2)
                while not self._stop.is_set() and not self._drop.is_set():
                    try:
                        data = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    self.received.extend(data)
            except OSError:
                pass
            finally:
                conn.close()

    def drop(self):
        self._drop.set()

    def close(self):
        self._stop.set()
        self._srv.close()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def device():
    d = FakeDevice()
    yield d
    d.close()


def test_connects_and_parses(device):
    c = gh_client.Client("127.0.0.1", device.port)
    c.start()
    try:
        assert wait_for(lambda: c.snapshot().switches.get("AS-84F-1"))
        assert c.snapshot().switches["AS-84F-1"].selected == "Beam-20"
    finally:
        c.stop()


def test_records_split_one_byte_at_a_time_still_parse():
    """The worst case the network can produce."""
    d = FakeDevice(chunk=1)
    c = gh_client.Client("127.0.0.1", d.port)
    c.start()
    try:
        # Wait on the last field to arrive, not the first: SWITCHADD registers
        # the switch well before SWITCHUPDATE fills in the selection.
        def ready():
            sw = c.snapshot().switches.get("AS-84F-1")
            return sw is not None and sw.selected

        assert wait_for(ready, 10.0)
        sw = c.snapshot().switches["AS-84F-1"]
        assert sw.selected == "Beam-20"
        assert sw.ports == ("Beam-20", "OFF")
    finally:
        c.stop()
        d.close()


def test_reconnects_and_keeps_last_state_visible(device):
    c = gh_client.Client("127.0.0.1", device.port)
    c.start()
    try:
        assert wait_for(lambda: c.snapshot().connected)
        device.drop()

        # State stays on screen, flagged stale, rather than blanking.
        assert wait_for(lambda: c.snapshot().stale)
        assert c.snapshot().switches["AS-84F-1"].selected == "Beam-20"

        assert wait_for(lambda: c.snapshot().connected, 10.0)
        assert device.connections >= 2
    finally:
        c.stop()


def test_client_sends_nothing_before_the_device_speaks(device):
    """The device transmits first; there is no client hello."""
    c = gh_client.Client("127.0.0.1", device.port, keepalive=False)
    c.start()
    try:
        assert wait_for(lambda: c.snapshot().connected)
        time.sleep(0.5)
        assert bytes(device.received) == b""
    finally:
        c.stop()


def test_keepalive_byte_is_nul():
    """Captured from GH Everyware Client: a single NUL, not a newline."""
    assert p.KEEPALIVE_BYTE == b"\x00"
    assert p.KEEPALIVE_INTERVAL == 5.0


def test_keepalive_can_be_switched_off(device):
    c = gh_client.Client("127.0.0.1", device.port, keepalive=False)
    c.start()
    try:
        assert wait_for(lambda: c.snapshot().connected)
        time.sleep(0.5)
        assert bytes(device.received) == b""
    finally:
        c.stop()


def test_keepalive_is_sent_on_the_configured_interval(device, monkeypatch):
    monkeypatch.setattr(p, "KEEPALIVE_INTERVAL", 0.05)
    c = gh_client.Client("127.0.0.1", device.port, keepalive=True)
    c.start()
    try:
        assert wait_for(lambda: len(device.received) >= 3, 5.0)
        assert set(bytes(device.received)) == set(p.KEEPALIVE_BYTE)
    finally:
        c.stop()


def test_unreachable_host_reports_an_error_without_dying():
    c = gh_client.Client("127.0.0.1", 1)  # nothing listens on port 1
    c.start()
    try:
        assert wait_for(lambda: c.snapshot().last_error)
        assert not c.snapshot().connected
    finally:
        c.stop()
