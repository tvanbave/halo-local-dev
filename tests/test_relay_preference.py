"""Automatic relay choice prefers this bridge's own paired lights. Synthetic only."""
import asyncio
from types import SimpleNamespace
import json
import tempfile
import unittest

import test_bootstrap_automatic as auto
from avionmesh_pi.bootstrap import PREFERRED_RELAY_MIN_RSSI, paired_relay_addresses
from avionmesh_pi.relay_settings import relay_status, scan_policy, save_preference, RelayConflict
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

    def test_persisted_preference_tracks_identity_and_handles_stale_edits(self):
        self.add('own', 32902, 'verified', OWN_UUID)
        runtime = SimpleNamespace(store=self.store, transport=auto.AutoBlueZTransport())
        selection = {'address': OWN, 'device_id': 32902}
        status = save_preference(runtime, {'revision': 'initial', 'preferred': selection})
        self.assertEqual(status['preferred'], OWN)
        self.assertIsNone(status['candidates'][0]['rssi'])
        with self.assertRaises(RelayConflict):
            save_preference(runtime, {'revision': 'initial', 'preferred': None})
        self.assertEqual(scan_policy(self.store), ({OWN}, OWN))
        self.store.set('retired_claims', ['own'])
        self.assertEqual(scan_policy(self.store), (set(), None))
        self.assertIsNone(relay_status(runtime)['preferred'])
        with self.assertRaises(RelayConflict):
            save_preference(runtime, {'revision': status['revision'], 'preferred': selection})
        self.assertIsNone(save_preference(runtime, {'revision': status['revision'], 'preferred': None})['preferred'])

    def test_ambiguous_addresses_do_not_get_a_name_or_preference(self):
        self.add('own', 32902, 'verified', OWN_UUID)
        self.add('conflicting', 32902, 'verified', '3c'+OWN_UUID[2:])
        self.assertEqual(scan_policy(self.store), (set(), None))


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

    async def test_user_preference_falls_back_and_returns_on_next_connection(self):
        self.transport.scan_policy = lambda: ({OWN, STRANGER}, OWN)
        self.advertise([(STRANGER, -45), (OWN, -90)])
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, OWN)
        await self.transport.disconnect()
        self.advertise([(STRANGER, -45)])
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, STRANGER)
        self.assertEqual(self.transport.attempted_preference, OWN)
        await self.transport.disconnect()
        self.advertise([(STRANGER, -45), (OWN, -65)])
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, OWN)

    async def test_preferred_advertises_but_connection_fails_then_connects_to_fallback(self):
        self.transport.scan_policy = lambda: ({OWN, STRANGER}, OWN)
        self.advertise([(STRANGER, -45), (OWN, -90)])
        self.scanner.find_device_by_address.side_effect = lambda address, **kwargs: (
            None if address == OWN else SimpleNamespace(address=address))
        await self.transport.connect()
        self.assertEqual([c.args[0] for c in self.scanner.find_device_by_address.await_args_list], [OWN, STRANGER])
        self.assertEqual(self.transport.selected_address, STRANGER)

    async def test_manual_scan_updates_samples_without_changing_connection(self):
        self.advertise([(OWN, -62), (STRANGER, -80)])
        await self.transport.connect()
        client = self.transport.client
        old = self.transport.observations[OWN]
        self.advertise([(STRANGER, -49)])
        await self.transport.scan(manual=True)
        self.assertIs(self.transport.client, client)
        self.assertEqual(self.transport.selected_address, OWN)
        self.assertEqual(self.transport.observations[OWN], old)
        self.assertEqual(self.transport.latest_candidates, {STRANGER})
        self.assertEqual(self.transport.observations[STRANGER][0], -49)
        self.advertise([])
        await self.transport.scan(manual=True)
        self.assertEqual(self.transport.latest_candidates, set())
        self.assertEqual(self.scanner.find_device_by_address.await_count, 1)
        self.assertEqual(self.transport.scan_number, 3)
        self.scanner.discover.side_effect = OSError('adapter busy')
        with self.assertRaises(OSError):
            await self.transport.scan(manual=True)
        self.assertEqual(self.transport.scan_number, 3)
        self.assertEqual(self.transport.selected_address, OWN)

    async def test_scan_refuses_overlapping_manual_requests_and_cancels_cleanly(self):
        started = asyncio.Event()
        async def pending(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.scanner.discover.side_effect = pending
        task = asyncio.create_task(self.transport.scan(manual=True))
        await started.wait()
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            await self.transport.scan(manual=True)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.scanner.discover.side_effect = None
        await self.transport.scan(manual=True)
        self.assertEqual(self.transport.scan_number, 1)
