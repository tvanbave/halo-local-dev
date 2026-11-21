"""Learn which device the Pi's Bluetooth relay is, from the hop count of its own messages.

Every mesh frame ends with a relay hop count that lights lower as they pass a message on
(see association.py). On the owner's installation on 2026-11-20 two different relays, an A19
and an RL4, each delivered their own authenticated replies with a hop count of exactly 50,
while every other light arrived at 49 or lower; the same A19 read 50 as the relay and 49 when
it was not. That lets the bridge name a relay it never paired itself, such as a cloud-imported
light.

The hop count is not covered by the message's authentication code, so one reading proves
nothing. An identity is recorded only after several distinct authenticated messages from one
known device arrive at exactly that count during one connection, with no other device reaching
it. A second device at that count during the same connection contradicts the observation, and
any identity recorded for the address is forgotten. Identities recorded here only name and rank
relays. They are never ownership, pairing or removal evidence, and a verified pairing record
always takes precedence (see bootstrap.relay_devices).
"""
import re
import time
from collections import Counter

from .bridge import CONTROLLERS, LIGHTS

OBSERVED_RELAYS = 'observed_relay_identities'
RELAY_HOP_COUNT = 50
REQUIRED_MESSAGES = 3
MAX_IDENTITIES = 128
_ADDRESS = re.compile(r'[0-9A-F]{2}(?::[0-9A-F]{2}){5}')


def connection_address(value):
    """The connected relay's address in canonical form, or None if it is not a usable address."""
    if not isinstance(value, str):
        return None
    value = value.upper()
    if not _ADDRESS.fullmatch(value) or value == '00:00:00:00:00:00':
        return None
    return value


class RelayIdentity:
    """Per-connection observer. The Bluetooth loop calls start() and stop() around each connection."""

    def __init__(self, store, product_of):
        self.store = store
        self.product_of = product_of  # mesh ID -> product ID of a current inventory device, or None
        self.stop()

    def stop(self):
        self.address = None
        self.counts = Counter()
        self.seen = set()
        self.contradicted = False
        self.recorded = None

    def start(self, address):
        self.stop()
        self.address = connection_address(address)

    def status(self):
        return {'address': self.address, 'recorded': self.recorded, 'contradicted': self.contradicted}

    def observe(self, source, sequence, hop_count):
        """Count one authenticated message. Returns the recorded device ID when an identity is saved."""
        if self.address is None or self.contradicted or hop_count != RELAY_HOP_COUNT:
            return None
        product = self.product_of(source)
        if product not in LIGHTS and product not in CONTROLLERS:
            return None
        key = (source, sequence)
        if key in self.seen:
            return None  # the same message heard twice is one piece of evidence
        if len(self.seen) < 1024:
            self.seen.add(key)
        self.counts[source] += 1
        if len(self.counts) > 1:
            # Two devices cannot both be the relay: trust neither, and drop what this address had.
            self.contradicted = True
            self.recorded = None
            self._forget(self.address)
            return None
        if self.recorded is None and self.counts[source] >= REQUIRED_MESSAGES:
            self._record(source, product)
            self.recorded = source
            return source
        return None

    def _entries(self):
        saved = self.store.get(OBSERVED_RELAYS, {})
        return dict(saved) if isinstance(saved, dict) else {}

    def _record(self, device_id, product_id):
        entries = self._entries()
        # A device has one Bluetooth address: the latest direct observation replaces an older one.
        entries = {a: e for a, e in entries.items()
                   if a != self.address and not (isinstance(e, dict) and e.get('device_id') == device_id)}
        entries[self.address] = {'device_id': device_id, 'product_id': product_id,
                                 'observed_at': int(time.time()), 'messages': self.counts[device_id],
                                 'evidence': 'relay_hop_count'}
        if len(entries) > MAX_IDENTITIES:
            keep = sorted(entries, key=lambda a: entries[a].get('observed_at', 0)
                          if isinstance(entries[a], dict) else 0)[-MAX_IDENTITIES:]
            entries = {a: entries[a] for a in keep}
        self.store.set(OBSERVED_RELAYS, entries)

    def _forget(self, address):
        entries = self._entries()
        if address in entries:
            del entries[address]
            self.store.set(OBSERVED_RELAYS, entries)
