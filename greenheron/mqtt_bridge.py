"""
MQTT bridge with Home Assistant discovery.

Each switch becomes an HA `select` entity whose options are the antenna names the
device advertises, so the four switches appear as one device with four dropdowns.
That is the entity type that actually matches the hardware -- a switch is on
exactly one of nine ports, which is a choice, not a set of toggles.

Topic layout (prefix configurable, default `greenheron`):

    greenheron/availability          online | offline   (retained, also the LWT)
    greenheron/as_84f_1/state        currently selected port    (retained)
    greenheron/as_84f_1/set          write a port name here to select it
    greenheron/as_84f_1/attributes   JSON: locks, holder, ports, raw telemetry

Discovery configs are published retained under `homeassistant/select/...`, so
Home Assistant repopulates after a restart without the bridge doing anything.

State is only ever published from what the device reports. A command is sent and
then forgotten; the resulting state topic comes from the device's own
confirmation ~123 ms later, never from optimism about what we just asked for.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Optional

from greenheron import client as gh_client
from greenheron import protocol as p

log = logging.getLogger("greenheron.mqtt")

DEFAULT_PREFIX = "greenheron"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_NODE_ID = "greenheron_switch"

ONLINE = "online"
OFFLINE = "offline"


def slug(name: str) -> str:
    """AS-84F-1 -> as_84f_1. Stable, and safe in a topic and a unique_id."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass
class Topics:
    prefix: str = DEFAULT_PREFIX
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX
    node_id: str = DEFAULT_NODE_ID

    @property
    def availability(self) -> str:
        return f"{self.prefix}/availability"

    def state(self, switch: str) -> str:
        return f"{self.prefix}/{slug(switch)}/state"

    def command(self, switch: str) -> str:
        return f"{self.prefix}/{slug(switch)}/set"

    def attributes(self, switch: str) -> str:
        return f"{self.prefix}/{slug(switch)}/attributes"

    def discovery(self, switch: str) -> str:
        return f"{self.discovery_prefix}/select/{self.node_id}/{slug(switch)}/config"

    @property
    def command_wildcard(self) -> str:
        return f"{self.prefix}/+/set"


class Bridge:
    """Glue between a greenheron Client and an MQTT client.

    The MQTT client is injected rather than constructed here so the whole bridge
    can be tested against a fake, and against a real broker, without changing
    anything.
    """

    def __init__(
        self,
        gh: gh_client.Client,
        mqtt_client,
        topics: Optional[Topics] = None,
        discovery: bool = True,
        device_name: str = "Green Heron antenna switch",
    ):
        self.gh = gh
        self.mqtt = mqtt_client
        self.topics = topics or Topics()
        self.discovery = discovery
        self.device_name = device_name

        self._lock = threading.Lock()
        self._announced: set[str] = set()
        self._published: dict[str, str] = {}
        self._available: Optional[bool] = None

        gh.on_change = self.on_panel

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.mqtt.subscribe(self.topics.command_wildcard, qos=1)
        # Nothing is known yet; say so until the device tells us otherwise.
        self._set_available(False)
        self.gh.start()

    def stop(self) -> None:
        self._set_available(False)
        self.gh.stop()

    # -- device -> mqtt ----------------------------------------------------

    def on_panel(self, panel) -> None:
        """Called from the client's reader thread."""
        with self._lock:
            for name in panel.order:
                sw = panel.switches[name]
                if self.discovery and sw.ports and name not in self._announced:
                    self._announce(name, sw.ports)
                    self._announced.add(name)

                if sw.selected and self._published.get(self.topics.state(name)) != sw.selected:
                    self._publish(self.topics.state(name), sw.selected, retain=True)

                held = panel.locks_by_switch(name)
                attrs = {
                    "switch": name,
                    "ports": list(sw.ports),
                    "locks": list(sw.locks),
                    # Which other switch holds each port, per SWITCHLOCKS.
                    "in_use_elsewhere": held,
                    # SWITCHUPDATE field 4. Meaning unknown -- passed through
                    # raw and deliberately given no unit or device_class.
                    "telemetry_raw": sw.telemetry,
                }
                self._publish(self.topics.attributes(name),
                              json.dumps(attrs, sort_keys=True), retain=True)

            self._set_available(panel.connected and not panel.stale)

    def _announce(self, switch: str, ports: tuple[str, ...]) -> None:
        cfg = {
            "name": switch,
            "unique_id": f"{self.topics.node_id}_{slug(switch)}",
            "object_id": f"{self.topics.node_id}_{slug(switch)}",
            "command_topic": self.topics.command(switch),
            "state_topic": self.topics.state(switch),
            "json_attributes_topic": self.topics.attributes(switch),
            "availability_topic": self.topics.availability,
            "payload_available": ONLINE,
            "payload_not_available": OFFLINE,
            "options": list(ports),
            # Never assume the command worked -- wait for the device to confirm.
            "optimistic": False,
            "icon": "mdi:antenna",
            "device": {
                "identifiers": [self.topics.node_id],
                "name": self.device_name,
                "manufacturer": "Green Heron Engineering",
                "model": "AS-84F",
            },
        }
        self._publish(self.topics.discovery(switch), json.dumps(cfg), retain=True)
        log.info("announced %s to Home Assistant (%d options)", switch, len(ports))

    def _set_available(self, available: bool) -> None:
        if self._available == available:
            return
        self._available = available
        self._publish(self.topics.availability,
                      ONLINE if available else OFFLINE, retain=True)

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self._published[topic] = payload
        self.mqtt.publish(topic, payload, qos=1, retain=retain)

    # -- mqtt -> device ----------------------------------------------------

    def on_command(self, topic: str, payload: str) -> None:
        """Handle a write to <prefix>/<slug>/set."""
        want = payload.strip()
        panel = self.gh.snapshot()

        match = None
        for name in panel.order:
            if topic == self.topics.command(name):
                match = name
                break
        if match is None:
            # Some brokers replay retained messages that do not match the
            # subscription filter, so anything that is not shaped like a command
            # topic is ordinary noise rather than a misconfiguration.
            if topic.endswith("/set"):
                log.warning("command for an unknown switch: %r", topic)
            else:
                log.debug("ignoring non-command topic %r", topic)
            return

        ports = panel.switches[match].ports
        if ports and want not in ports:
            log.warning("%s: %r is not one of %s", match, want, list(ports))
            return

        holder = panel.locks_by_switch(match).get(want)
        if holder:
            # Send anyway: the device is the authority and will simply refuse.
            # State stays truthful either way because it comes from the device.
            log.warning("%s: %s appears to be in use by %s", match, want, holder)

        try:
            wire = self.gh.select(match, want)
        except (RuntimeError, ConnectionError) as exc:
            log.error("%s: could not send %r: %s", match, want, exc)
            return
        log.info("%s -> %s (%d bytes)", match, want, len(wire))


# --------------------------------------------------------------------------
# paho wiring
# --------------------------------------------------------------------------

def build_mqtt_client(
    broker: str,
    port: int = 1883,
    username: Optional[str] = None,
    password: Optional[str] = None,
    client_id: str = "greenheron-bridge",
    tls: bool = False,
    will_topic: Optional[str] = None,
):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if username:
        client.username_pw_set(username, password)
    if tls:
        client.tls_set()
    if will_topic:
        # If this process dies, HA must stop trusting the retained state.
        client.will_set(will_topic, OFFLINE, qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def run(
    switch_host: str,
    switch_port: int = p.DEFAULT_PORT,
    broker: str = "localhost",
    broker_port: int = 1883,
    username: Optional[str] = None,
    password: Optional[str] = None,
    topics: Optional[Topics] = None,
    discovery: bool = True,
    dry_run: bool = False,
    tls: bool = False,
) -> int:
    import paho.mqtt.client as mqtt

    topics = topics or Topics()
    mqtt_client = build_mqtt_client(
        broker, broker_port, username, password,
        tls=tls, will_topic=topics.availability,
    )
    gh = gh_client.Client(switch_host, switch_port, dry_run=dry_run)
    bridge = Bridge(gh, mqtt_client, topics, discovery=discovery)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error("broker refused connection: %s", reason_code)
            return
        log.info("connected to broker %s:%s", broker, broker_port)
        client.subscribe(topics.command_wildcard, qos=1)
        # Re-assert everything: a reconnect may have lost session state.
        bridge._announced.clear()
        bridge._published.clear()
        bridge._available = None
        bridge.on_panel(gh.snapshot())

    def on_message(client, userdata, msg):
        try:
            bridge.on_command(msg.topic, msg.payload.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a bad message must not kill the bridge
            log.exception("failed handling %s", msg.topic)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    log.info("connecting to broker %s:%s", broker, broker_port)
    mqtt_client.connect(broker, broker_port, keepalive=60)
    mqtt_client.loop_start()
    bridge.start()

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        bridge.stop()
        mqtt_client.loop_stop()
        try:
            mqtt_client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return 0
