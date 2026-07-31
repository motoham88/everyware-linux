"""Bridge logic against a fake MQTT client. No broker, no hardware."""

import json

import pytest

from greenheron import client as gh_client
from greenheron import mqtt_bridge as mb
from greenheron import protocol as p

PORTS = ("Beam-20", "Dipole-6", "Dummy Load", "OFF")


def rec(*fields):
    return p.US.join(x.encode() for x in fields) + p.CRLF


class FakeMQTT:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []
        self.subscriptions: list[str] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def last(self, topic):
        for t, payload, _ in reversed(self.published):
            if t == topic:
                return payload
        return None

    def topics(self):
        return {t for t, _, _ in self.published}


@pytest.fixture
def rig():
    gh = gh_client.Client("192.0.2.1", dry_run=True)
    mqtt = FakeMQTT()
    bridge = mb.Bridge(gh, mqtt)
    return gh, mqtt, bridge


def feed(gh, bridge, *records):
    for r in records:
        gh._apply(p.parse(r.rstrip(p.CRLF)))
    bridge.on_panel(gh.snapshot())


def roster(gh, bridge):
    # Announcement order 1, 3, 2, 4 -- as the device actually sends it.
    feed(gh, bridge, *[rec("SWITCHADD", "1", f"AS-84F-{n}", *PORTS) for n in (1, 3, 2, 4)])


# -- topics ----------------------------------------------------------------

def test_slug_is_topic_safe():
    assert mb.slug("AS-84F-1") == "as_84f_1"
    assert mb.slug("Dummy Load") == "dummy_load"


def test_topic_layout():
    t = mb.Topics()
    assert t.state("AS-84F-1") == "greenheron/as_84f_1/state"
    assert t.command("AS-84F-1") == "greenheron/as_84f_1/set"
    assert t.availability == "greenheron/availability"
    assert t.discovery("AS-84F-1") == \
        "homeassistant/select/greenheron_switch/as_84f_1/config"


def test_command_wildcard_matches_every_command_topic():
    t = mb.Topics()
    assert t.command_wildcard == "greenheron/+/set"
    assert t.command("AS-84F-1").startswith(t.prefix + "/")


# -- discovery -------------------------------------------------------------

def test_discovery_published_once_per_switch(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    roster(gh, bridge)  # a second panel update must not re-announce
    configs = [t for t, _, _ in mqtt.published if t.startswith("homeassistant/")]
    assert len(configs) == 4
    assert len(set(configs)) == 4


def test_discovery_payload_is_a_select_with_the_real_options(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    cfg = json.loads(mqtt.last(bridge.topics.discovery("AS-84F-1")))
    assert cfg["options"] == list(PORTS)
    assert cfg["command_topic"] == "greenheron/as_84f_1/set"
    assert cfg["state_topic"] == "greenheron/as_84f_1/state"
    assert cfg["availability_topic"] == "greenheron/availability"
    assert cfg["unique_id"] == "greenheron_switch_as_84f_1"


def test_all_switches_share_one_ha_device(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    ids = {
        json.loads(mqtt.last(bridge.topics.discovery(f"AS-84F-{n}")))["device"]["identifiers"][0]
        for n in (1, 2, 3, 4)
    }
    assert ids == {"greenheron_switch"}


def test_discovery_is_not_optimistic(rig):
    """HA must wait for the device to confirm, like every other front end."""
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    cfg = json.loads(mqtt.last(bridge.topics.discovery("AS-84F-1")))
    assert cfg["optimistic"] is False


def test_discovery_declares_no_unit_for_the_unknown_telemetry(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    cfg = json.loads(mqtt.last(bridge.topics.discovery("AS-84F-1")))
    assert "unit_of_measurement" not in cfg
    assert "device_class" not in cfg


def test_discovery_can_be_disabled():
    gh = gh_client.Client("192.0.2.1", dry_run=True)
    mqtt = FakeMQTT()
    bridge = mb.Bridge(gh, mqtt, discovery=False)
    roster(gh, bridge)
    feed(gh, bridge, rec("SWITCHUPDATE", "AS-84F-1", "Beam-20", "0", "-27"))
    assert not [t for t in mqtt.topics() if t.startswith("homeassistant/")]
    # State still flows; only the HA announcement is suppressed.
    assert mqtt.last(bridge.topics.state("AS-84F-1")) == "Beam-20"


def test_state_is_not_published_before_the_device_reports_one(rig):
    """A roster alone says nothing about relay position -- publishing "" would
    make HA show an empty selection that was never real."""
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    assert bridge.topics.state("AS-84F-1") not in mqtt.topics()


# -- state -----------------------------------------------------------------

def test_state_is_published_retained(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    feed(gh, bridge, rec("SWITCHUPDATE", "AS-84F-1", "Beam-20", "0", "-27"))
    assert mqtt.last("greenheron/as_84f_1/state") == "Beam-20"
    assert [r for t, _, r in mqtt.published if t == "greenheron/as_84f_1/state"] == [True]


def test_unchanged_state_is_not_republished(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    for _ in range(3):
        feed(gh, bridge, rec("SWITCHUPDATE", "AS-84F-1", "Beam-20", "0", "-27"))
    hits = [t for t, _, _ in mqtt.published if t == "greenheron/as_84f_1/state"]
    assert len(hits) == 1


def test_attributes_carry_locks_and_raw_telemetry(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    feed(gh, bridge,
         rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"),
         rec("SWITCHLOCKS", "AS-84F-1", "OFF", "OFF", "Dipole-6", "OFF"))
    attrs = json.loads(mqtt.last("greenheron/as_84f_1/attributes"))
    assert attrs["telemetry_raw"] == ""  # AS-84F-1 has had no SWITCHUPDATE
    # Slot 2 is AS-84F-2 under announcement order, not AS-84F-3.
    assert attrs["in_use_elsewhere"] == {"Dipole-6": "AS-84F-2"}
    assert attrs["ports"] == list(PORTS)


# -- availability ----------------------------------------------------------

def test_availability_starts_offline_and_goes_online(rig):
    gh, mqtt, bridge = rig
    bridge._set_available(False)
    assert mqtt.last("greenheron/availability") == "offline"
    roster(gh, bridge)
    gh._panel.__dict__["connected"] = True
    bridge.on_panel(gh.snapshot())
    assert mqtt.last("greenheron/availability") == "online"


def test_stale_connection_marks_entities_unavailable(rig):
    from dataclasses import replace
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    gh._panel = replace(gh._panel, connected=True, stale=False)
    bridge.on_panel(gh.snapshot())
    assert mqtt.last("greenheron/availability") == "online"

    gh._panel = replace(gh._panel, connected=False, stale=True)
    bridge.on_panel(gh.snapshot())
    assert mqtt.last("greenheron/availability") == "offline"


def test_availability_is_not_republished_when_unchanged(rig):
    gh, mqtt, bridge = rig
    for _ in range(3):
        bridge._set_available(False)
    hits = [t for t, _, _ in mqtt.published if t == "greenheron/availability"]
    assert len(hits) == 1


# -- commands --------------------------------------------------------------

def test_command_sends_the_exact_wire_bytes(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    bridge.on_command("greenheron/as_84f_4/set", "Beam-20")
    assert gh.sent_log == [b"SET_SWITCH\x1fAS-84F-4\x1fBeam-20\r\n"]


def test_command_payload_is_stripped(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    bridge.on_command("greenheron/as_84f_4/set", "  Beam-20\n")
    assert gh.sent_log == [b"SET_SWITCH\x1fAS-84F-4\x1fBeam-20\r\n"]


def test_command_with_a_port_name_containing_a_space(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    bridge.on_command("greenheron/as_84f_1/set", "Dummy Load")
    assert gh.sent_log == [b"SET_SWITCH\x1fAS-84F-1\x1fDummy Load\r\n"]


def test_unknown_port_is_rejected_without_transmitting(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    bridge.on_command("greenheron/as_84f_1/set", "Beam-99")
    assert gh.sent_log == []


def test_unknown_topic_is_ignored(rig):
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    bridge.on_command("greenheron/nope/set", "Beam-20")
    assert gh.sent_log == []


def test_command_does_not_publish_state_optimistically(rig):
    """State must come from the device, never from the command we just sent."""
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    feed(gh, bridge, rec("SWITCHUPDATE", "AS-84F-1", "OFF", "0", "-27"))
    bridge.on_command("greenheron/as_84f_1/set", "Beam-20")
    assert mqtt.last("greenheron/as_84f_1/state") == "OFF"


def test_locked_port_is_still_attempted(rig):
    """The device is the authority -- the bridge warns but does not veto."""
    gh, mqtt, bridge = rig
    roster(gh, bridge)
    feed(gh, bridge,
         rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"),
         rec("SWITCHLOCKS", "AS-84F-1", "OFF", "OFF", "Dipole-6", "OFF"))
    bridge.on_command("greenheron/as_84f_1/set", "Dipole-6")
    assert gh.sent_log == [b"SET_SWITCH\x1fAS-84F-1\x1fDipole-6\r\n"]
