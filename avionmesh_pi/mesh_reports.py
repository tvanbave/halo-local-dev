"""How far each device's latest message travelled through the mesh, for display only.

Every mesh frame ends with a hop count that lights lower as they pass a message on. Lights
send their own messages at 50: on the owner's installation on 2026-11-20 two different
relays, an A19 and an RL4, each delivered their own replies at exactly 50, while every other
light arrived at 49 or lower (see relay_identity). So for a light, 50 minus the received
count is the number of lights that passed the message on; the connected relay itself is 0.

Controllers start from a different, unconfirmed count (native keypad commands arrived at 18
and 19), so only the raw count is kept for them. The count is outside the message's
authentication code, and copies of one message can arrive over several paths with different
counts; the highest count seen for a message is kept, since that is its shortest path.
These readings are never ownership, identity or delivery evidence.
"""
from .bridge import LIGHTS
from .relay_identity import RELAY_HOP_COUNT, connection_address

LIGHT_ORIGIN_HOP_COUNT = RELAY_HOP_COUNT


class MeshReports:
    def __init__(self, capacity=512):
        self.capacity = capacity
        self.latest = {}

    def observe(self, source, sequence, hop_count, received_ms, relay):
        """Record one authenticated message; never raises on unexpected input."""
        if (type(source) is not int or type(sequence) is not int or type(hop_count) is not int
                or not 0 <= hop_count <= 255 or type(received_ms) is not int):
            return
        relay = connection_address(relay)
        current = self.latest.get(source)
        if current is not None and current['sequence'] == sequence and current['relay'] == relay:
            # Another copy of the same message: keep its shortest path.
            current['hop_count'] = max(current['hop_count'], hop_count)
            return
        if current is None and len(self.latest) >= self.capacity:
            oldest = min(self.latest, key=lambda key: self.latest[key]['received_ms'])
            del self.latest[oldest]
        self.latest[source] = {'sequence': sequence, 'hop_count': hop_count,
                               'received_ms': received_ms, 'relay': relay}

    def view(self, devices, now, current_relay):
        """Readings for known devices. ``devices`` maps device id to product id."""
        current_relay = connection_address(current_relay)
        result = {}
        for source, reading in self.latest.items():
            product = devices.get(source)
            if product is None:
                continue
            count = reading['hop_count']
            hops = (LIGHT_ORIGIN_HOP_COUNT - count
                    if product in LIGHTS and count <= LIGHT_ORIGIN_HOP_COUNT else None)
            result[str(source)] = {'hop_count': count, 'hops': hops,
                                   'observed_ms': reading['received_ms'],
                                   'age_ms': max(0, now - reading['received_ms']),
                                   'relay_address': reading['relay'],
                                   'via_current_relay': current_relay is not None
                                                        and reading['relay'] == current_relay}
        return result
