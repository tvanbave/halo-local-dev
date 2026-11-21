"""Relay identity learned from the hop count of the connected relay's own messages. Synthetic only."""
import json
import tempfile
import unittest
from types import SimpleNamespace

import test_bootstrap_automatic as auto
from avionmesh_pi.bootstrap import relay_devices
from avionmesh_pi.protocol import encrypt_mcp
from avionmesh_pi.relay_identity import (MAX_IDENTITIES, OBSERVED_RELAYS, RELAY_HOP_COUNT, REQUIRED_MESSAGES,
                                         RelayIdentity, connection_address)
from avionmesh_pi.relay_settings import RelayConflict, relay_status, save_preference, scan_policy
from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_service import KEY

RELAY = '02:00:00:00:00:A1'
OTHER = '02:00:00:00:00:A2'
CLAIMED_UUID = 'f7000000000214000004000000000000'   # address 02:00:00:00:00:F7
CLAIMED = '02:00:00:00:00:F7'


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        # Imported devices: no claim journals, so no Bluetooth identity until observed.
        self.store.import_inventory({'devices': [
            {'device_id': 32902, 'product_id': 134, 'name': 'Living room lamp', 'exposed': False},
            {'device_id': 32903, 'product_id': 93, 'name': 'Closet 1', 'exposed': False},
            {'device_id': 32904, 'product_id': 93, 'name': 'Closet 2', 'exposed': False},
            {'device_id': 33000, 'product_id': 127, 'name': 'Hall keypad', 'exposed': False}], 'groups': []})
        self.products = {d['device_id']: d['product_id'] for d in self.store.inventory()['devices']}
        self.observer = RelayIdentity(self.store, self.products.get)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def feed(self, source, count, hop=RELAY_HOP_COUNT, first=1):
        return [self.observer.observe(source, sequence, hop) for sequence in range(first, first + count)]

    def claim(self, target, uuid, product=93):
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                              ('claim-' + str(target), 'claim', target, 'verified',
                               json.dumps({'uuid': uuid, 'product_id': product})))


class ObserverTests(StoreCase):
    def test_records_after_enough_distinct_messages_at_the_relay_count(self):
        self.observer.start(RELAY.lower())
        self.assertEqual(self.feed(32902, REQUIRED_MESSAGES - 1), [None] * (REQUIRED_MESSAGES - 1))
        self.assertEqual(self.observer.observe(32902, REQUIRED_MESSAGES, RELAY_HOP_COUNT), 32902)
        entry = self.store.get(OBSERVED_RELAYS)[RELAY]
        self.assertEqual((entry['device_id'], entry['product_id'], entry['evidence']), (32902, 134, 'relay_hop_count'))
        self.assertEqual(relay_devices(self.store)[RELAY],
                         {'device_id': 32902, 'name': 'Living room lamp', 'kind': 'light', 'identity': 'observed'})
        self.assertEqual(self.observer.status(), {'address': RELAY, 'recorded': 32902, 'contradicted': False})

    def test_repeats_lower_counts_and_unknown_sources_are_not_evidence(self):
        self.observer.start(RELAY)
        self.feed(32902, 5, first=7)
        self.observer.observe(32902, 7, RELAY_HOP_COUNT)        # the same message heard again
        self.feed(32903, 10, hop=RELAY_HOP_COUNT - 1)            # forwarded by the relay
        self.feed(40000, 10)                                      # not in the inventory
        self.feed(0x8000, 10)                                     # the bridge's own address
        self.assertEqual(self.observer.recorded, 32902)
        self.observer.start(OTHER)
        self.observer.observe(32903, 1, RELAY_HOP_COUNT)
        self.observer.observe(32903, 1, RELAY_HOP_COUNT)
        self.observer.observe(32903, 2, RELAY_HOP_COUNT)
        self.assertIsNone(self.observer.recorded)

    def test_a_second_device_at_the_relay_count_contradicts_and_forgets(self):
        self.observer.start(RELAY)
        self.feed(32902, REQUIRED_MESSAGES)
        self.assertIn(RELAY, self.store.get(OBSERVED_RELAYS))
        self.observer.observe(32903, 1, RELAY_HOP_COUNT)
        self.assertTrue(self.observer.contradicted)
        self.assertNotIn(RELAY, self.store.get(OBSERVED_RELAYS))
        self.feed(32902, 10, first=50)
        self.assertNotIn(RELAY, self.store.get(OBSERVED_RELAYS))
        self.assertNotIn(RELAY, relay_devices(self.store))

    def test_mixed_devices_before_the_threshold_record_nothing(self):
        self.observer.start(RELAY)
        self.feed(32902, REQUIRED_MESSAGES - 1)
        self.feed(32903, 1)
        self.feed(32902, 10, first=100)
        self.assertEqual(self.store.get(OBSERVED_RELAYS, {}), {})

    def test_each_connection_starts_fresh_and_stopping_ends_observation(self):
        self.observer.start(RELAY)
        self.feed(32902, REQUIRED_MESSAGES - 1)
        self.observer.start(OTHER)
        self.feed(32902, 1, first=500)
        self.assertEqual(self.store.get(OBSERVED_RELAYS, {}), {})
        self.observer.stop()
        self.feed(32902, 10, first=600)
        self.assertEqual(self.store.get(OBSERVED_RELAYS, {}), {})

    def test_unusable_connection_addresses_are_never_recorded(self):
        for address in (None, '', '00:00:00:00:00:00', 'not an address', 42):
            self.assertIsNone(connection_address(address))
            self.observer.start(address)
            self.feed(32902, 10)
        self.assertEqual(self.store.get(OBSERVED_RELAYS, {}), {})

    def test_latest_observation_replaces_an_older_address_or_device(self):
        self.observer.start(RELAY)
        self.feed(32902, REQUIRED_MESSAGES)
        self.observer.start(OTHER)
        self.feed(32902, REQUIRED_MESSAGES)
        self.assertEqual(set(self.store.get(OBSERVED_RELAYS)), {OTHER})
        self.observer.start(OTHER)
        self.feed(32903, REQUIRED_MESSAGES)
        self.assertEqual(self.store.get(OBSERVED_RELAYS)[OTHER]['device_id'], 32903)

    def test_saved_identities_are_bounded(self):
        self.store.set(OBSERVED_RELAYS, {f'02:00:00:00:{i // 256:02X}:{i % 256:02X}':
                                         {'device_id': 32904, 'product_id': 93, 'observed_at': i}
                                         for i in range(MAX_IDENTITIES)})
        self.observer.start(RELAY)
        self.feed(32902, REQUIRED_MESSAGES)
        saved = self.store.get(OBSERVED_RELAYS)
        self.assertEqual(len(saved), MAX_IDENTITIES)
        self.assertIn(RELAY, saved)


class MergeTests(StoreCase):
    def observe(self, address, device):
        self.observer.start(address)
        self.feed(device, REQUIRED_MESSAGES)

    def test_imported_light_becomes_selectable_and_preferred(self):
        self.observe(RELAY, 32902)
        runtime = SimpleNamespace(store=self.store, transport=auto.AutoBlueZTransport())
        runtime.transport.observations = {RELAY: (-66, 1700000000)}
        status = relay_status(runtime)
        row = next(r for r in status['candidates'] if r['address'] == RELAY)
        self.assertEqual((row['name'], row['identity']), ('Living room lamp', 'observed'))
        self.assertEqual(scan_policy(self.store), ({RELAY}, None, set()))
        saved = save_preference(runtime, {'revision': 'initial', 'preferred': {'address': RELAY, 'device_id': 32902}})
        self.assertEqual(saved['preferred'], RELAY)
        self.assertEqual(scan_policy(self.store)[1], RELAY)

    def test_observed_controllers_join_the_controller_list(self):
        self.observe(RELAY, 33000)
        self.assertEqual(relay_devices(self.store)[RELAY]['kind'], 'controller')
        self.assertEqual(scan_policy(self.store), (set(), None, {RELAY}))

    def test_a_pairing_record_wins_over_an_observation(self):
        self.claim(32903, CLAIMED_UUID)
        self.observe(CLAIMED, 32902)           # same address, different device
        self.observe(OTHER, 32903)             # claimed device seen at another address
        devices = relay_devices(self.store)
        self.assertEqual(devices[CLAIMED]['device_id'], 32903)
        self.assertEqual(devices[CLAIMED]['identity'], 'pairing')
        self.assertNotIn(OTHER, devices)

    def test_removed_changed_or_malformed_observations_are_ignored(self):
        self.store.set(OBSERVED_RELAYS, {
            RELAY: {'device_id': 32999, 'product_id': 93},                   # no longer in the inventory
            OTHER: {'device_id': 32903, 'product_id': 134},                  # model changed
            '02:00:00:00:00:A3': {'device_id': '32904', 'product_id': 93},   # wrong type
            '02:00:00:00:00:A4': 'junk', 'not an address': {'device_id': 32904, 'product_id': 93},
            '02:00:00:00:00:A5': {'device_id': 32904, 'product_id': 93},
            '02:00:00:00:00:A6': {'device_id': 32904, 'product_id': 93}})    # ambiguous: two addresses
        self.assertEqual(relay_devices(self.store), {})
        self.store.set(OBSERVED_RELAYS, 'junk')
        self.assertEqual(relay_devices(self.store), {})
        runtime = SimpleNamespace(store=self.store, transport=auto.AutoBlueZTransport())
        with self.assertRaises(RelayConflict):
            save_preference(runtime, {'revision': 'initial', 'preferred': {'address': RELAY, 'device_id': 32999}})


class RuntimeTransport:
    ready = True
    address = RELAY

    async def send(self, *_args, **_kwargs):
        return True


class RuntimeTests(StoreCase):
    def test_received_frames_feed_the_observer_and_status(self):
        runtime = Runtime(self.store, KEY, RuntimeTransport(), clock=lambda: 10)
        runtime.mesh_ready = True
        runtime.relay_identity.start(RuntimeTransport.address)
        reply = b'\x00\x80\x73\x00\x0a\x00\x80'

        def frame(source, sequence, hop):
            return encrypt_mcp(reply, KEY, sequence, source)[:-1] + bytes([hop])
        for sequence in range(1, REQUIRED_MESSAGES + 1):
            runtime.observe(frame(32902, sequence, RELAY_HOP_COUNT), 10)
            runtime.observe(frame(32903, 100 + sequence, RELAY_HOP_COUNT - 1), 10)
        self.assertEqual(runtime.relay_identity.recorded, 32902)
        self.assertEqual(relay_devices(self.store)[RELAY]['device_id'], 32902)

    def test_a_failing_observer_never_disturbs_receiving(self):
        runtime = Runtime(self.store, KEY, RuntimeTransport(), clock=lambda: 10)
        runtime.mesh_ready = True

        def broken(*_args):
            raise RuntimeError('synthetic')
        runtime.relay_identity.observe = broken
        before = runtime.receive_count
        runtime.observe(encrypt_mcp(b'\x00\x80\x73\x00\x0a\x00\x80', KEY, 1, 32902), 10)
        self.assertEqual(runtime.receive_count, before + 1)
        self.assertEqual(runtime.core.counts['relay_identity_failed'], 1)


if __name__ == '__main__':
    unittest.main()
