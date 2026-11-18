"""Automatic relay choice prefers this bridge's own paired lights. Synthetic only."""
import copy
import json
import tempfile
import unittest

import test_bootstrap_automatic as auto
from avionmesh_pi.bootstrap import PREFERRED_RELAY_MIN_RSSI, paired_relay_addresses
from support import synthetic_store
from test_service import KEY

OWN_UUID = 'f7000000000214000004000000000000'   # address 02:00:00:00:00:F7
OWN = '02:00:00:00:00:F7'
STRANGER = '02:00:00:00:00:3C'


class PairedRelayAddressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [{'device_id': 32902, 'product_id': 93, 'name': 'Own', 'exposed': False}],
                                     'groups': []})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, ident, target, status, uuid, product=93):
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                              (ident, 'claim', target, status, json.dumps({'uuid': uuid, 'product_id': product})))

    def test_only_verified_claims_still_in_inventory(self):
        self.add('own', 32902, 'verified', OWN_UUID)
        self.add('retired', 32901, 'retired', '3c000000000214000004000000000000')
        self.add('removed', 32950, 'verified', 'aa000000000214000004000000000000')
        self.add('pending', 32951, 'uncertain', 'bb000000000214000004000000000000')
        self.add('junk', 32902, 'verified', 'not-hex' * 4 + 'abcd')
        self.assertEqual(paired_relay_addresses(self.store), {OWN})

    def test_controller_claims_never_enter_light_preference(self):
        data = self.store.inventory()
        for index, product in enumerate((81, 91, 126, 127)):
            target = 33000 + index
            data['devices'].append({'device_id': target, 'product_id': product,
                                    'name': 'Controller', 'exposed': False})
            identity = ('%02x' % (index + 1)) + OWN_UUID[2:]
            self.add('controller-' + str(product), target, 'verified', identity, product)
        self.store.import_inventory(data)
        self.add('own', 32902, 'verified', OWN_UUID)
        self.assertEqual(paired_relay_addresses(self.store), {OWN})

    def test_missing_or_changed_claim_model_is_not_preferred(self):
        self.add('mismatched', 32902, 'verified', OWN_UUID, 134)
        self.add('unknown', 32902, 'verified', OWN_UUID, None)
        self.assertEqual(paired_relay_addresses(self.store), set())

    def test_retired_verified_receipt_is_not_preferred(self):
        self.add('old', 32902, 'verified', OWN_UUID)
        self.store.set('retired_claims', ['old'])
        self.assertEqual(paired_relay_addresses(self.store), set())

    def test_zero_bluetooth_address_is_not_preferred(self):
        self.add('zero', 32902, 'verified', '00' * 16)
        self.assertEqual(paired_relay_addresses(self.store), set())

    def test_incomplete_uuid_padded_with_whitespace_is_not_preferred(self):
        self.add('short', 32902, 'verified', OWN_UUID[:12] + ' ' * 20)
        self.assertEqual(paired_relay_addresses(self.store), set())


class RelayChoiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = auto.AutomaticTransportTests.asyncSetUp
    asyncTearDown = auto.AutomaticTransportTests.asyncTearDown

    def advertise(self, rows):
        self.scanner.discover.return_value = {i: auto.advertisement(a, rssi) for i, (a, rssi) in enumerate(rows)}

    async def test_own_light_beats_stronger_stranger(self):
        self.advertise([(STRANGER, -45), (OWN, -70)])
        self.transport.preferred_relays = lambda: {OWN}
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, OWN)
        self.assertIs(self.transport.relay_preferred, True)

    async def test_weak_own_light_does_not_beat_usable_stranger(self):
        self.advertise([(STRANGER, -45), (OWN, PREFERRED_RELAY_MIN_RSSI - 1)])
        self.transport.preferred_relays = lambda: {OWN}
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, STRANGER)
        self.assertIs(self.transport.relay_preferred, False)

    async def test_without_own_light_strongest_is_used(self):
        self.advertise([(STRANGER, -45), ('02:00:00:00:00:33', -60)])
        self.transport.preferred_relays = lambda: {OWN}
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, STRANGER)

    async def test_broken_preference_never_blocks_connecting(self):
        self.advertise([(STRANGER, -45), (OWN, -70)])
        def broken():
            raise RuntimeError('synthetic store failure')
        self.transport.preferred_relays = broken
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, STRANGER)

    async def test_failed_own_light_falls_back_within_bounded_set(self):
        self.advertise([(STRANGER, -45), (OWN, -70)])
        self.transport.preferred_relays = lambda: {OWN}
        def lookup(address, **kwargs):
            return None if address == OWN else auto.SimpleNamespace(address=address)
        self.scanner.find_device_by_address.side_effect = lookup
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, STRANGER)
