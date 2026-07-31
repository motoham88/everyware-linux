"""GTK4 panel tests.

Skipped where PyGObject or a display is unavailable -- the protocol and client
suites cover everything headless. These check the layer that turns device state
into widget state, which is where a GUI actually goes wrong.
"""

import os

import pytest

gi = pytest.importorskip("gi", reason="PyGObject not installed")

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display available", allow_module_level=True)

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

from greenheron import client as gh_client  # noqa: E402
from greenheron import gui  # noqa: E402
from greenheron import protocol as p  # noqa: E402

Gtk.init()

PORTS = ("Beam-10", "Beam-20", "Dipole-6", "OFF")


def rec(*fields):
    return p.US.join(x.encode() for x in fields) + p.CRLF


@pytest.fixture(scope="session")
def app():
    """One application for the whole run.

    Registering the same application_id twice collides on the session bus, so
    this cannot be per-test. NON_UNIQUE also keeps it from attaching to a real
    panel that happens to be running on the desktop.
    """
    a = Gtk.Application(application_id="org.greenheron.Test",
                        flags=Gio.ApplicationFlags.NON_UNIQUE)
    a.register()
    return a


@pytest.fixture
def win(app):
    client = gh_client.Client("192.0.2.1", dry_run=True)
    # Announcement order 1, 3, 2, 4 -- as the real device sends it.
    for n in (1, 3, 2, 4):
        client._apply(p.parse(rec("SWITCHADD", "1", f"AS-84F-{n}", *PORTS).rstrip(p.CRLF)))
    w = gui.PanelWindow(app, client, "192.0.2.10:10000")
    w.client = client
    yield w
    w.destroy()


def feed(win, *records):
    for r in records:
        win.client._apply(p.parse(r.rstrip(p.CRLF)))
    win.update(win.client.snapshot())


def cell(win, switch, port):
    return win._buttons[(switch, port)]


def classes(btn):
    return {c[5:] for c in btn.get_css_classes() if c.startswith("port-")}


# --------------------------------------------------------------------------

def test_grid_columns_are_sorted_not_announcement_order(win):
    win.update(win.client.snapshot())
    assert win._built_for == ("AS-84F-1", "AS-84F-2", "AS-84F-3", "AS-84F-4")


def test_selected_port_is_marked_and_stays_clickable(win):
    feed(win, rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"))
    btn = cell(win, "AS-84F-2", "Dipole-6")
    assert classes(btn) == {"selected"}
    assert btn.get_sensitive()


def test_locked_port_is_attributed_by_announcement_order(win):
    """Slot 2 belongs to AS-84F-2, which the device announces third."""
    feed(win,
         rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"),
         rec("SWITCHLOCKS", "AS-84F-1", "OFF", "OFF", "Dipole-6", "OFF"))
    btn = cell(win, "AS-84F-1", "Dipole-6")
    assert classes(btn) == {"locked"}
    assert "AS-84F-2" in btn.get_label()
    # Sorted order would have blamed AS-84F-3.
    assert "AS-84F-3" not in btn.get_label()


def test_locked_port_cannot_be_clicked(win):
    feed(win,
         rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"),
         rec("SWITCHLOCKS", "AS-84F-1", "OFF", "OFF", "Dipole-6", "OFF"))
    assert not cell(win, "AS-84F-1", "Dipole-6").get_sensitive()


def test_the_holder_can_still_click_its_own_selection(win):
    feed(win,
         rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"),
         rec("SWITCHLOCKS", "AS-84F-2", "OFF", "OFF", "Dipole-6", "OFF"))
    assert cell(win, "AS-84F-2", "Dipole-6").get_sensitive()


def test_deselecting_clears_the_marking(win):
    feed(win, rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"))
    assert classes(cell(win, "AS-84F-2", "Dipole-6")) == {"selected"}
    feed(win, rec("SWITCHUPDATE", "AS-84F-2", "OFF", "0", "-27"))
    assert classes(cell(win, "AS-84F-2", "Dipole-6")) == set()


def test_clicking_sends_the_exact_wire_bytes(win):
    win.update(win.client.snapshot())
    cell(win, "AS-84F-4", "Beam-20").emit("clicked")
    assert win.client.sent_log == [b"SET_SWITCH\x1fAS-84F-4\x1fBeam-20\r\n"]


def test_click_does_not_optimistically_mark_the_button(win):
    """The device is the authority; the UI must wait to be told."""
    win.update(win.client.snapshot())
    cell(win, "AS-84F-4", "Beam-20").emit("clicked")
    win.update(win.client.snapshot())
    assert classes(cell(win, "AS-84F-4", "Beam-20")) == set()


def test_stale_state_disables_the_grid(win):
    from dataclasses import replace
    panel = replace(win.client.snapshot(), connected=False, stale=True)
    win.update(panel)
    assert not win.grid.get_sensitive()
    assert "status-warn" in win.status.get_css_classes()


def test_wireless_signal_is_parsed_but_not_displayed(win):
    """It reports the Green Heron wireless link, not antenna state, so it has no
    place in the switch grid -- but it must still parse."""
    feed(win, rec("SWITCHUPDATE", "AS-84F-1", "OFF", "0", "-27"))
    assert win.client.snapshot().switches["AS-84F-1"].wireless_signal == "-27"
    assert not hasattr(win, "telemetry")
