"""End-to-end against a real MQTT broker.

Runs an in-process amqtt broker, points the bridge at it with real paho, and
subscribes with a second independent client. This exercises the parts a fake
cannot: retained messages, the last-will, wildcard subscriptions, and the actual
paho callback signatures.

Skipped if amqtt or paho are unavailable -- the fake-client suite covers the
logic either way.
"""

import asyncio
import itertools
import json
import socket
import threading
import time

import pytest

pytest.importorskip("amqtt", reason="amqtt not installed")
pytest.importorskip("paho.mqtt", reason="paho-mqtt not installed")

import paho.mqtt.client as mqtt  # noqa: E402
from amqtt.broker import Broker  # noqa: E402

from greenheron import client as gh_client  # noqa: E402
from greenheron import mqtt_bridge as mb  # noqa: E402
from greenheron import protocol as p  # noqa: E402

PORTS = ("Beam-20", "Dipole-6", "Dummy Load", "OFF")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rec(*fields):
    return p.US.join(x.encode() for x in fields) + p.CRLF


class BrokerThread:
    """amqtt is asyncio; run it on its own loop in a thread."""

    def __init__(self, port):
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.broker = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def boot():
            # Broker.__init__ calls asyncio.get_running_loop(), so it has to be
            # constructed from inside the loop, not merely started there.
            # Keep this config minimal. Adding "plugins": {} or a "topic-check"
            # key makes amqtt accept the TCP connection and then drop it during
            # the MQTT handshake, which looks exactly like a network problem.
            self.broker = Broker({
                "listeners": {"default": {
                    "type": "tcp", "bind": f"127.0.0.1:{self.port}",
                }},
                "sys_interval": 0,
                "auth": {"allow-anonymous": True},
            })
            await self.broker.start()

        self.loop.run_until_complete(boot())
        self.loop.call_soon(self.ready.set)
        self.loop.run_forever()

    def start(self):
        self.thread.start()
        assert self.ready.wait(15), "broker did not start"
        time.sleep(0.4)

    def stop(self):
        try:
            asyncio.run_coroutine_threadsafe(self.broker.shutdown(), self.loop).result(10)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


_ids = itertools.count()


def unique_id(stem: str) -> str:
    """MQTT brokers evict an existing session with the same client_id, so every
    client in these tests needs its own -- otherwise a later test silently
    disconnects an earlier one's subscriber."""
    return f"{stem}-{next(_ids)}"


class Collector:
    """A second, independent MQTT client -- what Home Assistant would be."""

    def __init__(self, port):
        self.messages: dict[str, str] = {}
        self.lock = threading.Lock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=unique_id("collector"))
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port, 30)
        self.client.subscribe("#", qos=1)
        self.client.loop_start()

    def _on_message(self, client, userdata, msg):
        with self.lock:
            self.messages[msg.topic] = msg.payload.decode("utf-8", "replace")

    def get(self, topic):
        with self.lock:
            return self.messages.get(topic)

    def wait_for(self, topic, timeout=10.0, predicate=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            val = self.get(topic)
            if val is not None and (predicate is None or predicate(val)):
                return val
            time.sleep(0.05)
        return None

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture(scope="module")
def broker():
    port = free_port()
    b = BrokerThread(port)
    b.start()
    yield port
    b.stop()


@pytest.fixture
def rig(broker):
    collector = Collector(broker)
    time.sleep(0.3)

    gh = gh_client.Client("192.0.2.1", dry_run=True)
    topics = mb.Topics()
    client = mb.build_mqtt_client("127.0.0.1", broker,
                                  client_id=unique_id("bridge"),
                                  will_topic=topics.availability)
    client.connect("127.0.0.1", broker, 30)
    client.loop_start()
    bridge = mb.Bridge(gh, client, topics)

    def on_message(c, u, msg):
        bridge.on_command(msg.topic, msg.payload.decode())

    client.on_message = on_message
    client.subscribe(topics.command_wildcard, qos=1)
    time.sleep(0.3)

    # Roster in the device's real announcement order.
    for n in (1, 3, 2, 4):
        gh._apply(p.parse(rec("SWITCHADD", "1", f"AS-84F-{n}", *PORTS).rstrip(p.CRLF)))
    yield gh, bridge, collector, broker

    client.loop_stop()
    client.disconnect()
    collector.stop()


def publish(gh, bridge, *records):
    for r in records:
        gh._apply(p.parse(r.rstrip(p.CRLF)))
    bridge.on_panel(gh.snapshot())


# --------------------------------------------------------------------------

def test_discovery_reaches_a_real_subscriber(rig):
    gh, bridge, collector, _ = rig
    bridge.on_panel(gh.snapshot())
    raw = collector.wait_for("homeassistant/select/greenheron_switch/as_84f_1/config")
    assert raw is not None, "no discovery config arrived"
    cfg = json.loads(raw)
    assert cfg["options"] == list(PORTS)
    assert cfg["optimistic"] is False


def test_state_round_trips_through_the_broker(rig):
    gh, bridge, collector, _ = rig
    publish(gh, bridge, rec("SWITCHUPDATE", "AS-84F-1", "Beam-20", "0", "-27"))
    assert collector.wait_for("greenheron/as_84f_1/state") == "Beam-20"


def test_attributes_arrive_as_valid_json(rig):
    gh, bridge, collector, _ = rig
    publish(gh, bridge,
            rec("SWITCHUPDATE", "AS-84F-2", "Dipole-6", "0", "-27"),
            rec("SWITCHLOCKS", "AS-84F-1", "OFF", "OFF", "Dipole-6", "OFF"))
    # Match on the field under test, not a substring: "Dipole-6" also appears in
    # the ports list, so a looser predicate accepts a stale retained payload
    # left on the broker by an earlier test.
    raw = collector.wait_for(
        "greenheron/as_84f_1/attributes",
        predicate=lambda v: json.loads(v).get("in_use_elsewhere"),
    )
    assert raw is not None
    attrs = json.loads(raw)
    assert attrs["in_use_elsewhere"] == {"Dipole-6": "AS-84F-2"}


def test_a_command_published_by_a_third_party_reaches_the_device(rig):
    """This is the path Home Assistant actually uses."""
    gh, bridge, collector, broker_port = rig
    sender = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=unique_id("ha-sim"))
    sender.connect("127.0.0.1", broker_port, 30)
    sender.loop_start()
    sender.publish("greenheron/as_84f_4/set", "Beam-20", qos=1)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not gh.sent_log:
        time.sleep(0.05)
    sender.loop_stop()
    sender.disconnect()

    assert gh.sent_log == [b"SET_SWITCH\x1fAS-84F-4\x1fBeam-20\r\n"]


def test_retained_state_is_delivered_to_a_late_subscriber(rig):
    """HA restarting must not have to wait for the next device update."""
    gh, bridge, collector, broker_port = rig
    publish(gh, bridge, rec("SWITCHUPDATE", "AS-84F-1", "Dipole-6", "0", "-27"))
    assert collector.wait_for("greenheron/as_84f_1/state",
                              predicate=lambda v: v == "Dipole-6")

    late = Collector(broker_port)
    try:
        assert late.wait_for("greenheron/as_84f_1/state",
                             predicate=lambda v: v == "Dipole-6") == "Dipole-6"
    finally:
        late.stop()
