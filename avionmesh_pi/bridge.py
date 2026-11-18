"""Authenticated frame -> MQTT intent pipeline; no network client is imported."""
from collections import Counter, OrderedDict
from dataclasses import dataclass
import re

from .protocol import decode_controller, decode_light, decrypt_mcp, integer, plan_light_command
from .controllers import ControllerRouter

CONTROLLERS = {81: "Anyplace Dimmer", 91: "Accessory Dimmer",
               126: "Battery Scene Keypad", 127: "Wired Scene Keypad"}
LIGHTS = {90: "Lamp Dimmer", 93: "RL Downlight", 94: "Light Adapter", 97: "Smart Dimmer",
          134: "A19 Bulb", 137: "Surface Downlight", 162: "MicroEdge", 167: "Smart Switch"}
COLOR_LIGHTS = (93, 134, 137, 162)
EVENT_TYPES = ["on", "off", "brightness", "raise", "lower", "mesh_command"]


@dataclass(frozen=True)
class Device:
    device_id: int
    product_id: int
    name: str
    exposed: bool = False

    def __post_init__(self):
        integer(self.device_id, 0x8080, 65407, "device ID")
        integer(self.product_id, 0, 255, "product ID")
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 128:
            raise ValueError("Invalid device name")
        if type(self.exposed) is not bool:
            raise ValueError("Invalid exposure flag")


def load_devices(records):
    if not isinstance(records, list) or len(records)>512:
        raise ValueError("Expected a devices list")
    devices = []
    for record in records:
        if not isinstance(record, dict) or set(record) - {"device_id", "product_id", "name", "exposed"}:
            raise ValueError("Expected public device fields only; private inventories are unsupported")
        try:
            devices.append(Device(**record))
        except TypeError:
            raise ValueError("Missing device fields") from None
    if len({d.device_id for d in devices}) != len(devices):
        raise ValueError("Duplicate device ID")
    return devices


class Bridge:
    def __init__(self, devices, key, node="pi_lab"):
        if not isinstance(node, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", node):
            raise ValueError("Invalid node namespace")
        if len(key) != 16:
            raise ValueError("MCP key must contain 16 bytes")
        self.devices = {d.device_id: d for d in devices}
        if len(self.devices) != len(devices):
            raise ValueError("Duplicate device ID")
        self._key = key
        self.node = node
        self.prefix = "avionmesh_pi/" + node
        self.ready_since = None
        self.last_ms = -1
        self.seen = OrderedDict()
        self.states = {}
        self.counts = Counter()
        self.controllers = ControllerRouter()

    def _clock(self, now_ms):
        integer(now_ms, 0, 2**63 - 1, "timestamp")
        if now_ms < self.last_ms:
            raise ValueError("Replay clock moved backwards")
        self.last_ms = now_ms

    def intent(self, topic, payload, retain=False):
        return {"mode": "offline_intent", "topic": topic, "payload": payload,
                "retain": retain, "qos": 0}

    def discovery(self):
        messages = []
        for d in self.devices.values():
            if not d.exposed or d.product_id not in {**CONTROLLERS, **LIGHTS}:
                continue
            controller = d.product_id in CONTROLLERS
            component = "event" if controller else "light"
            uid = f"{self.node}_{d.device_id}"
            topic = f"{self.prefix}/{component}/{d.device_id}"
            config = {"name": d.name, "unique_id": uid,
                      "state_topic": topic + ("/event" if controller else "/state"),
                      "availability": [{"topic": self.prefix + "/availability"},
                                       {"topic": self.prefix + "/mesh/availability"}],
                      "availability_mode": "all",
                      "device": {"identifiers": [uid], "manufacturer": "HALO / Avi-on",
                                 "model": (CONTROLLERS if controller else LIGHTS)[d.product_id]}}
            if controller:
                config["event_types"] = EVENT_TYPES.copy()
            else:
                config.update(schema="json", command_topic=topic + "/set",
                              brightness=d.product_id != 167, brightness_scale=255,
                              supported_color_modes=["color_temp"] if d.product_id in COLOR_LIGHTS else ["onoff"] if d.product_id == 167 else ["brightness"])
                if d.product_id in COLOR_LIGHTS:
                    config.update(color_temp_kelvin=True, min_kelvin=2700, max_kelvin=5000)
                config["availability"].append({"topic": topic + "/availability"})
            messages.append(self.intent(f"homeassistant/{component}/{uid}/config", config, True))
        return messages

    def connection(self, ready, now_ms):
        self._clock(now_ms)
        if type(ready) is not bool:
            raise ValueError("Expected boolean ready state")
        self.ready_since = now_ms if ready else None
        self.states.clear()  # fresh readbacks required in each new session
        self.controllers.reset()
        messages = [self.intent(self.prefix + suffix, "online" if ready else "offline", True)
                    for suffix in ("/availability", "/mesh/availability")]
        messages.extend(self.availability(now_ms))
        return messages

    def availability(self, now_ms):
        self._clock(now_ms)
        result = []
        for d in self.devices.values():
            if not d.exposed or d.product_id not in LIGHTS:
                continue
            brightness = self.states.get(d.device_id, {}).get("brightness")
            fresh = (self.ready_since is not None and brightness is not None
                     and now_ms - brightness[1] < 120_000)
            result.append(self.intent(f"{self.prefix}/light/{d.device_id}/availability",
                                      "online" if fresh else "offline", True))
        return result

    def receive(self, frame, received_ms, now_ms, *, controller_since=None, controller_observer=None):
        """Decode once, with an optional local-only controller observation seam.

        Default replay/publication behavior is unchanged. A runtime may supply a
        current mesh-session boundary and observer to accept known controllers
        independently of HA exposure/MQTT; that never enables MQTT publication.
        """
        self._clock(now_ms)
        integer(received_ms, 0, 2**63 - 1, "receipt timestamp")
        if controller_since is not None:
            integer(controller_since, 0, 2**63 - 1, "controller session timestamp")
        publish_ready = self.ready_since is not None and received_ms >= self.ready_since
        observe_ready = (controller_since is not None and controller_observer is not None
                         and received_ms >= controller_since)
        if not 0 <= now_ms - received_ms <= 1000 or not (publish_ready or observe_ready):
            self.counts["not_ready_or_stale"] += 1
            return []
        try:
            packet = decrypt_mcp(frame, self._key)
        except ValueError:
            self.counts["invalid_frame"] += 1
            return []
        d = self.devices.get(packet.crypto_source)
        if (d is None or not (publish_ready and d.exposed)
                and not (observe_ready and d.product_id in CONTROLLERS)):
            self.counts["unknown_or_unexposed_source"] += 1
            return []
        # Exact authenticated envelope includes sequence; identical holds with
        # different sequences survive. This is relay dedup, not anti-replay.
        while self.seen and now_ms - next(iter(self.seen.values())) >= 5000:
            self.seen.popitem(last=False)
        replay_key = frame[:-1]  # mutable trailer is not packet identity
        if replay_key in self.seen:
            self.counts["duplicate"] += 1
            return []
        self.seen[replay_key] = now_ms
        if len(self.seen) > 64:
            self.seen.popitem(last=False)
        if d.product_id in CONTROLLERS:
            event = self.controllers.decode(packet, d.product_id, received_ms)
            if event:
                self.counts["controller_events"] += 1
                if observe_ready:
                    controller_observer({key: value for key, value in event.items()
                        if key in ('event_type', 'source', 'target', 'button', 'button_mapping',
                                   'activation', 'brightness', 'direction', 'delta', 'input_mode')}, received_ms)
                return ([self.intent(f"{self.prefix}/event/{d.device_id}/event", event)]
                        if publish_ready and d.exposed else [])
        elif d.product_id in LIGHTS:
            update = decode_light(packet)
            if update and "color_temp" in update and d.product_id not in COLOR_LIGHTS:
                update = None
            if update:
                state = self.states.setdefault(d.device_id, {})
                state.update({k: (v, received_ms) for k, v in update.items()})
                fresh = {k: v for k, (v, when) in state.items() if now_ms - when < 120_000}
                if "brightness" in fresh:
                    self.counts["light_reports"] += 1
                    if d.product_id in COLOR_LIGHTS and "color_temp" in fresh:
                        fresh["color_mode"] = "color_temp"
                    topic = f"{self.prefix}/light/{d.device_id}"
                    return [self.intent(topic + "/state", fresh, True),
                            self.intent(topic + "/availability", "online", True)]
        self.counts["ignored_packet"] += 1
        return []

    def plan_command(self, device_id, *, brightness=None, kelvin=None, retained=False):
        if retained:
            raise ValueError("Retained commands must never be executed")
        integer(device_id, 0x8080, 65407, "device ID")
        d = self.devices.get(device_id)
        if d is None or not d.exposed or d.product_id not in LIGHTS:
            raise ValueError("Expected an exposed known load device")
        if kelvin is not None and d.product_id not in COLOR_LIGHTS:
            raise ValueError("Device does not support color temperature")
        if d.product_id == 167 and brightness not in (0,255):
            raise ValueError('Switch supports only ON/OFF')
        return plan_light_command(device_id, brightness=brightness, kelvin=kelvin)
