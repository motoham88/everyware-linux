"""State-tracking tests. No sockets: records are fed straight to Client._apply."""

import pytest

from greenheron import protocol as p
from greenheron.client import Client, _sort_key


def feed(client, *records: bytes):
    for raw in records:
        client._apply(p.parse(raw.rstrip(p.CRLF)))


def add(switch, *ports):
    body = p.US.join(x.encode() for x in ("SWITCHADD", "1", switch, *ports))
    return body + p.CRLF


def update(switch, selected, tele="-27"):
    return p.US.join(
        x.encode() for x in ("SWITCHUPDATE", switch, selected, "0", tele)
    ) + p.CRLF


def locks(switch, *slots):
    return p.US.join(x.encode() for x in ("SWITCHLOCKS", switch, *slots)) + p.CRLF


@pytest.fixture
def client():
    return Client("192.0.2.1")


# --------------------------------------------------------------------------

def test_roster_and_selection_are_tracked(client):
    feed(client, add("AS-84F-1", "Beam-20", "OFF"), update("AS-84F-1", "Beam-20"))
    sw = client.snapshot().switches["AS-84F-1"]
    assert sw.ports == ("Beam-20", "OFF")
    assert sw.selected == "Beam-20"
    assert sw.telemetry == "-27"


def test_switch_order_is_stable_regardless_of_arrival_order(client):
    """The real device announced its roster as 1, 3, 2, 4.

    Arrival order must not decide column layout or lock slot mapping.
    """
    for n in (1, 3, 2, 4):
        feed(client, add(f"AS-84F-{n}", "OFF"))
    assert client.snapshot().order == [
        "AS-84F-1", "AS-84F-2", "AS-84F-3", "AS-84F-4",
    ]


def test_numeric_suffixes_sort_numerically_not_lexically():
    names = ["AS-84F-10", "AS-84F-2", "AS-84F-1"]
    assert sorted(names, key=_sort_key) == ["AS-84F-1", "AS-84F-2", "AS-84F-10"]


def test_unnumbered_names_sort_last_without_crashing():
    names = ["Rotator", "AS-84F-2", "AS-84F-1"]
    assert sorted(names, key=_sort_key)[-1] == "Rotator"


def test_update_before_add_does_not_lose_the_switch(client):
    """Ordering on the wire is not guaranteed; a lone UPDATE must still register."""
    feed(client, update("AS-84F-9", "Beam-15"))
    assert client.snapshot().switches["AS-84F-9"].selected == "Beam-15"


def test_add_does_not_clobber_a_known_selection(client):
    feed(client, update("AS-84F-1", "Beam-20"), add("AS-84F-1", "Beam-20", "OFF"))
    assert client.snapshot().switches["AS-84F-1"].selected == "Beam-20"


def test_unknown_records_are_ignored_by_the_state_model(client):
    feed(client, add("AS-84F-1", "OFF"), b"SOMETHINGNEW\x1fx\x1fy\r\n")
    assert client.snapshot().order == ["AS-84F-1"]


# -- locks -----------------------------------------------------------------

def test_locks_are_indexed_by_announcement_order_not_sorted_order(client):
    """The device announces 1, 3, 2, 4 and indexes lock slots that way.

    Verified on hardware: Beam-15 selected on AS-84F-3 landed in slot 1, and
    AS-84F-3 is announced second. Sorted order would predict slot 2.
    """
    for n in (1, 3, 2, 4):
        feed(client, add(f"AS-84F-{n}", "Beam-15", "EFHW-40", "OFF"))
    feed(client, locks("AS-84F-1", "OFF", "Beam-15", "EFHW-40", "OFF"))

    held = client.snapshot().locks_by_switch("AS-84F-1")
    assert held == {"Beam-15": "AS-84F-3", "EFHW-40": "AS-84F-2"}


def test_display_order_stays_sorted_even_though_locks_are_not(client):
    for n in (1, 3, 2, 4):
        feed(client, add(f"AS-84F-{n}", "OFF"))
    snap = client.snapshot()
    assert snap.order == ["AS-84F-1", "AS-84F-2", "AS-84F-3", "AS-84F-4"]
    assert snap.announced == ["AS-84F-1", "AS-84F-3", "AS-84F-2", "AS-84F-4"]


def test_replayed_roster_does_not_reorder_lock_slots(client):
    """A reconnect makes the device resend every SWITCHADD."""
    for n in (1, 3, 2, 4):
        feed(client, add(f"AS-84F-{n}", "OFF"))
    for n in (4, 2, 3, 1):  # a different order on the second pass
        feed(client, add(f"AS-84F-{n}", "OFF"))
    assert client.snapshot().announced == [
        "AS-84F-1", "AS-84F-3", "AS-84F-2", "AS-84F-4",
    ]


def test_a_switch_does_not_lock_itself(client):
    feed(client, add("AS-84F-1", "Beam-20"), add("AS-84F-2", "Beam-20"))
    feed(client, locks("AS-84F-1", "Beam-20", "OFF"))
    assert client.snapshot().locks_by_switch("AS-84F-1") == {}


def test_off_is_not_treated_as_a_lock(client):
    feed(client, add("AS-84F-1", "OFF"), add("AS-84F-2", "OFF"))
    feed(client, locks("AS-84F-1", "OFF", "OFF"))
    assert client.snapshot().locks_by_switch("AS-84F-1") == {}


def test_more_lock_slots_than_switches_does_not_crash(client):
    feed(client, add("AS-84F-1", "Beam-20"))
    feed(client, locks("AS-84F-1", "OFF", "Beam-20", "Beam-15", "EFHW-40"))
    assert client.snapshot().locks_by_switch("AS-84F-1") == {}


# -- sending ---------------------------------------------------------------

def test_select_encodes_exactly_what_the_official_client_sends():
    """Byte-for-byte against a capture of GH Everyware Client on AS-84F-4."""
    c = Client("192.0.2.1", dry_run=True)
    assert c.select("AS-84F-4", "Beam-20") == b"SET_SWITCH\x1fAS-84F-4\x1fBeam-20\r\n"
    assert c.select("AS-84F-4", "Dummy Load") == b"SET_SWITCH\x1fAS-84F-4\x1fDummy Load\r\n"
    assert c.select("AS-84F-4", "OFF") == b"SET_SWITCH\x1fAS-84F-4\x1fOFF\r\n"
    assert [len(w) for w in c.sent_log] == [29, 32, 25]


def test_select_refuses_when_the_command_is_unknown(client, monkeypatch):
    """The guard that kept guessed bytes off the wire before SET_SWITCH was known."""
    monkeypatch.setattr("greenheron.client.SELECT", None)
    feed(client, add("AS-84F-1", "Beam-20"))
    with pytest.raises(RuntimeError, match="not known yet"):
        client.select("AS-84F-1", "Beam-20")


def test_dry_run_logs_without_a_socket():
    c = Client("192.0.2.1", dry_run=True)
    wire = c.select("AS-84F-1", "Beam-20")
    assert c.sent_log == [wire]
    assert len(wire) == 29


def test_select_never_updates_local_state():
    """The device is the only authority on relay position."""
    c = Client("192.0.2.1", dry_run=True)
    feed(c, add("AS-84F-1", "Beam-20", "OFF"), update("AS-84F-1", "OFF"))
    c.select("AS-84F-1", "Beam-20")
    assert c.snapshot().switches["AS-84F-1"].selected == "OFF"


def test_locks_match_the_captured_response_to_a_real_selection(client):
    """Verbatim from hardware: Dipole-6 selected on AS-84F-2 appeared in slot 2
    of every switch's SWITCHLOCKS -- AS-84F-2 being announced third."""
    for n in (1, 3, 2, 4):  # the device's actual announcement order
        feed(client, add(f"AS-84F-{n}", "Dipole-6", "OFF"))
    for n in (1, 2, 3, 4):
        feed(client, locks(f"AS-84F-{n}", "OFF", "OFF", "Dipole-6", "OFF"))
    feed(client, update("AS-84F-2", "Dipole-6", "-27"))

    assert client.snapshot().locks_by_switch("AS-84F-1") == {"Dipole-6": "AS-84F-2"}
    # The holder does not see its own selection as a lock.
    assert client.snapshot().locks_by_switch("AS-84F-2") == {}


def test_snapshot_is_isolated_from_later_mutation(client):
    feed(client, update("AS-84F-1", "OFF"))
    snap = client.snapshot()
    feed(client, update("AS-84F-1", "Beam-20"))
    assert snap.switches["AS-84F-1"].selected == "OFF"
