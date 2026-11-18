import copy
import json
import tempfile
import unittest

from avionmesh_pi.association import uuid_hash
from avionmesh_pi.lifecycle import Lifecycle
from avionmesh_pi.protocol import decrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY, INVENTORY, FakeTransport

UUID = '00112233445566778899aabbccddee01'

class Scan:
    def __init__(self):
        self.calls = 0
        self.results = []
    async def discover(self):
        self.calls += 1
        return self.results

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda:10)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities['removal'] = True
        self.runtime.session()
        self.scan = Scan()
        self.lifecycle = Lifecycle(self.runtime, self.scan)
        self.claim = self.store.begin('claim',40002, {'uuid':UUID,'uuid_hash':uuid_hash(bytes.fromhex(UUID)),
                                                   'product_id':134})
        self.store.transition(self.claim,'prepared','attempted')
        self.store.transition(self.claim,'attempted','verified')
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_removal_requires_new_exact_uuid_scan_and_retains_reservations(self):
        before = copy.deepcopy(self.store.inventory())
        result = await self.lifecycle.remove(40002)
        packet = decrypt_mcp(self.transport.frames[0], KEY)
        self.assertEqual((packet.opcode, packet.payload), (4,bytes(10)))
        self.assertEqual(self.store.inventory(), before)
        with self.assertRaises(RuntimeError):
            await self.runtime.control(40002,{'state':'ON'})
        with self.assertRaises(RuntimeError):
            await self.lifecycle.remove(40002)
        self.assertFalse((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.scan.results = [{'uuid':UUID,'uuid_hash':uuid_hash(bytes.fromhex(UUID))}]
        self.assertTrue((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.assertEqual(self.scan.calls,2)
        self.assertIn(self.claim,self.store.get('retired_claims'))
        self.assertEqual(self.store.get('retired_addresses'),[40002])
        self.assertEqual(len(self.transport.frames),1)
        self.assertEqual(self.store.journals()[-1]['detail']['backup'],before)
        self.assertTrue(self.store.get('discovery_tombstones'))
        with self.assertRaises(ValueError):
            self.store.import_inventory(before)
        self.assertTrue((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.assertEqual(self.scan.calls,2)

    async def test_wrong_uuid_with_same_hash_cannot_complete(self):
        result = await self.lifecycle.remove(40002)
        self.scan.results = [{'uuid':'ff'*16,'uuid_hash':uuid_hash(bytes.fromhex(UUID))}]
        self.assertFalse((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.assertIn(40002,self.runtime.core.devices)

    async def test_failed_dispatch_is_uncertain_and_not_retried_after_restart(self):
        self.transport.fail = True
        with self.assertRaises(RuntimeError):
            await self.lifecycle.remove(40002)
        self.store.close()
        self.store = Store(self.temp.name,KEY)
        self.assertEqual(self.store.journals()[-1]['status'],'uncertain')
        self.assertEqual(self.store.journals()[-1]['detail']['uuid'],UUID)

    async def test_missing_identity_refuses_before_write(self):
        with self.assertRaises(ValueError):
            await self.lifecycle.remove(40001)
        self.assertEqual(self.transport.frames,[])

    async def test_preview_is_read_only_even_when_removal_disabled(self):
        self.runtime.capabilities['removal'] = False
        before = copy.deepcopy(self.store.journals())
        preview = self.lifecycle.preview_remove(40002)
        self.assertTrue(preview['eligible'])
        self.assertFalse(preview['enabled'])
        self.assertEqual([g['group_id'] for g in preview['affected_groups']], [291])
        self.assertFalse(preview['ha_automation_dependencies_verified'])
        self.assertEqual(self.store.journals(), before)
        self.assertEqual(self.transport.frames, [])

    async def test_product_identity_or_duplicate_claim_refuses_before_write(self):
        original = self.store.journals()[0]
        for defect in ('product', 'uuid', 'hash', 'retired', 'duplicate'):
            with self.subTest(defect=defect):
                detail = copy.deepcopy(original['detail'])
                if defect == 'product': detail['product_id'] = 91
                if defect == 'uuid': detail['uuid'] = 'zz' * 16
                if defect == 'hash': detail['uuid_hash'] += 1
                self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), self.claim))
                self.store.set('retired_claims', [self.claim] if defect == 'retired' else [])
                if defect == 'duplicate':
                    self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                        ('duplicate', 'claim', 40002, 'verified', json.dumps(detail)))
                with self.assertRaises(ValueError):
                    await self.lifecycle.remove(40002)
                self.assertEqual(self.transport.frames, [])
                self.store.db.execute("DELETE FROM journals WHERE id='duplicate'")

    async def test_review_changes_are_rejected_before_journal_or_write(self):
        preview = self.lifecycle.preview_remove(40002)
        inventory = self.store.inventory()
        inventory['devices'][1]['name'] = 'Different reviewed name'
        self.store.set('inventory', inventory)
        with self.assertRaisesRegex(ValueError, 'preview again'):
            await self.lifecycle.remove(40002, expected_revision=preview['revision'])
        self.assertEqual(len(self.store.journals()), 1)
        self.assertEqual(self.transport.frames, [])

    async def test_active_relay_requires_separate_reconnection(self):
        self.transport.address = ':'.join(f'{b:02X}' for b in bytes.fromhex(UUID)[:6][::-1])
        self.assertIn('active_relay_requires_reconnection', self.lifecycle.preview_remove(40002)['blockers'])
        with self.assertRaises(ValueError):
            await self.lifecycle.remove(40002)
        self.assertEqual(self.transport.frames, [])

    async def test_retained_direct_binding_blocks_even_after_session_invalidated(self):
        self.store.save_binding(40001, {'slots': [{'slot': 1, 'target': 40002}]})
        self.runtime.session()
        preview = self.lifecycle.preview_remove(40002)
        self.assertIn('native_controller_targets_device', preview['blockers'])
        self.assertEqual(preview['native_dependencies'],
                         [{'controller': 40001, 'destination': 40002, 'direct': True}])
        with self.assertRaises(ValueError):
            await self.lifecycle.remove(40002)
        self.assertEqual(self.transport.frames, [])

    async def test_last_light_in_native_controlled_group_blocks(self):
        self.store.observe_target(291, 40001)
        preview = self.lifecycle.preview_remove(40002)
        self.assertIn('native_controller_group_would_be_empty', preview['blockers'])
        with self.assertRaises(ValueError):
            await self.lifecycle.remove(40002)
        self.assertEqual(self.transport.frames, [])

    async def test_dispatch_admission_rechecks_identity_without_resending(self):
        allocate = self.store.allocate_sequence
        def replace_identity():
            sequence = allocate()
            self.store.set('retired_claims', [self.claim])
            return sequence
        self.store.allocate_sequence = replace_identity
        with self.assertRaises(ValueError):
            await self.lifecycle.remove(40002)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        with self.assertRaises(RuntimeError):
            await self.lifecycle.remove(40002)

    async def test_reconciliation_revalidates_identity_after_scan(self):
        result = await self.lifecycle.remove(40002)
        async def changed_claim():
            self.store.set('retired_claims', [self.claim])
            return [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        self.scan.discover = changed_claim
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile(result['transaction'])
        self.assertIn(40002, self.runtime.core.devices)
        self.assertEqual(len(self.transport.frames), 1)

    async def test_reconciliation_rejects_session_changed_during_scan(self):
        result = await self.lifecycle.remove(40002)
        async def boundary():
            self.runtime.session()
            return [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        self.scan.discover = boundary
        with self.assertRaises(RuntimeError):
            await self.lifecycle.reconcile(result['transaction'])
        self.assertIn(40002, self.runtime.core.devices)
        self.assertEqual(len(self.transport.frames), 1)

    async def test_duplicate_exact_scan_cannot_remove(self):
        result = await self.lifecycle.remove(40002)
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}] * 2
        self.assertFalse((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.assertIn(40002, self.runtime.core.devices)

    async def test_cleanup_removes_room_and_membership_preserves_other_rooms_and_reservations(self):
        self.store.set('device_rooms', {'40001': 'Control room', '40002': 'Light room'})
        self.store.set('local_modes', [40002])
        self.runtime.local_light_states[40002] = {'state': (True, 10)}
        self.store.reserve_targets([292, 24575])
        result = await self.lifecycle.remove(40002)
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        self.assertTrue((await self.lifecycle.reconcile(result['transaction']))['removed'])
        self.assertEqual(self.store.get('device_rooms'), {'40001': 'Control room'})
        self.assertEqual(self.store.get('local_modes'), [])
        self.assertNotIn(40002, self.runtime.local_light_states)
        self.assertEqual(self.store.inventory()['groups'][0]['members'], [])
        self.assertTrue(self.store.inventory()['groups'][1]['protected'])
        self.assertIn(24575, self.store.get('known_targets'))

    async def test_native_setup_receipt_protects_last_light_without_live_observation(self):
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
            ('native', 'accessory_native_setup', 40001, 'verified', json.dumps({
                'reservation': self.store.inventory()['groups'][0]})))
        preview = self.lifecycle.preview_remove(40002)
        self.assertIn('native_controller_group_would_be_empty', preview['blockers'])
        self.assertEqual(preview['native_dependencies'][0]['controller'], 40001)

    async def test_removal_receipt_changed_during_scan_cannot_retire(self):
        result = await self.lifecycle.remove(40002)
        async def changed_receipt():
            self.store.db.execute('UPDATE journals SET target=40001 WHERE id=?', (result['transaction'],))
            return [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        self.scan.discover = changed_receipt
        with self.assertRaisesRegex(ValueError, 'state changed'):
            await self.lifecycle.reconcile(result['transaction'])
        self.assertIn(40002, self.runtime.core.devices)

    async def test_native_setup_owner_reset_keeps_ordinary_group(self):
        inventory = self.store.inventory()
        inventory['devices'][1]['product_id'] = 91
        self.store.set('inventory', inventory)
        from avionmesh_pi.lifecycle import reload_inventory
        reload_inventory(self.runtime)
        detail = self.store.journals()[0]['detail']
        detail['product_id'] = 91
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), self.claim))
        setup = self.store.begin('accessory_native_setup', 40002, {
            'claim': self.claim, 'product_id': 91, 'assignment_attempted': True,
            'reservation': inventory['groups'][0]})
        self.store.transition(setup, 'prepared', 'attempted')
        self.store.transition(setup, 'attempted', 'uncertain')
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        result = await self.lifecycle.reconcile_reset(setup, True)
        self.assertTrue(result['removed'])
        self.assertEqual(self.store.inventory()['groups'][0]['group_id'], 291)
        self.assertEqual(self.store.journals()[1]['status'], 'retired')
        self.assertEqual(self.transport.frames, [])

    async def test_publication_failure_retains_confirmed_removal_and_cleanup_only_recovery(self):
        from unittest.mock import patch
        from avionmesh_pi.management import recovery_advice
        result = await self.lifecycle.remove(40002)
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        with patch('avionmesh_pi.lifecycle.reload_inventory', side_effect=OSError('synthetic publication failure')):
            outcome = await self.lifecycle.reconcile(result['transaction'])
        self.assertTrue(outcome['removed'])
        self.assertTrue(outcome['publication_pending'])
        self.assertTrue(outcome['requires_review'])
        receipt = next(j for j in self.store.journals() if j['id'] == result['transaction'])
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(recovery_advice(receipt)['actions'], ['refresh_removed_publication'])
        self.assertNotIn(40002, [d['device_id'] for d in self.store.inventory()['devices']])
        self.assertIn(40002, self.runtime.core.devices)  # failed before runtime refresh
        self.assertFalse(self.runtime.mesh_ready)
        scan_count, writes = self.scan.calls, len(self.transport.frames)
        outcome = await self.lifecycle.reconcile(result['transaction'])
        self.assertFalse(outcome['publication_pending'])
        self.assertNotIn(40002, self.runtime.core.devices)
        self.assertEqual((self.scan.calls, len(self.transport.frames)), (scan_count, writes))
        self.assertEqual(recovery_advice(self.store.journals()[-1])['actions'], [])

    async def test_pending_publication_survives_restart_and_waits_for_other_operation(self):
        from unittest.mock import patch
        result = await self.lifecycle.remove(40002)
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        with patch.object(self.runtime, 'session', side_effect=RuntimeError('synthetic sink failure')):
            outcome = await self.lifecycle.reconcile(result['transaction'])
        self.assertTrue(outcome['removed'])
        self.assertTrue(outcome['publication_pending'])
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.runtime = Runtime(self.store, KEY, FakeTransport(), clock=lambda:10)
        self.lifecycle = Lifecycle(self.runtime, self.scan)
        other = self.store.begin('keypad_edit', 40001, {})
        with self.assertRaisesRegex(RuntimeError, 'Another operation'):
            await self.lifecycle.reconcile(result['transaction'])
        self.store.transition(other, 'prepared', 'cancelled')
        scans = self.scan.calls
        result = await self.lifecycle.reconcile(result['transaction'])
        self.assertTrue(result['removed'])
        self.assertFalse(result['publication_pending'])
        self.assertEqual(self.scan.calls, scans)
        self.assertEqual(self.runtime.transport.frames, [])

    async def test_broker_outage_removal_restart_clears_discovery_then_repair_uses_fresh_address(self):
        from avionmesh_pi.commissioning import Commissioning
        from test_claim_development import MultiTransport
        from test_association import Peer
        old_topic = 'homeassistant/light/pi_lab_40002/config'
        self.assertTrue(next(m for m in self.runtime.discovery() if m['topic'] == old_topic)['payload'])
        self.runtime.mqtt_connection(False)
        result = await self.lifecycle.remove(40002)
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        await self.lifecycle.reconcile(result['transaction'])
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.runtime = Runtime(self.store, KEY, FakeTransport(), clock=lambda:10)
        output = []
        self.runtime.sink = output.append
        self.runtime.mqtt_connection(True)
        self.runtime.emit(self.runtime.discovery())
        deleted = [m for m in output if m['topic'] == old_topic]
        self.assertTrue(deleted)
        self.assertTrue(all(m['payload'] == '' and m['retain'] for m in deleted))
        self.assertTrue(next(m for m in output if m['topic'] == 'homeassistant/event/pi_lab_40001_button_1/config')['payload'])
        # Complete an independent ECDH association of the same physical UUID.
        transport = MultiTransport(self.runtime)
        transport.ads = [UUID]
        transport.peers = {uuid_hash(bytes.fromhex(UUID)): Peer()}
        self.runtime.transport = transport
        self.runtime.mesh_ready = True
        claims = Commissioning(self.runtime)
        await claims.discover(seconds=0)
        paired = await claims.claim(UUID, 'Re-paired light', 134, inventory_complete=True)
        self.assertTrue(paired['associated'])
        self.assertGreater(paired['device_id'], 40002)
        self.assertEqual(transport.starts, [uuid_hash(bytes.fromhex(UUID))])
        self.assertIn(40002, self.store.get('retired_addresses'))
        self.assertEqual(self.store.inventory()['groups'][0]['members'], [])
        self.assertFalse(self.runtime.core.devices[paired['device_id']].exposed)
        self.assertTrue(self.store.inventory()['groups'][1]['protected'])
        self.assertTrue(all(m['payload'] == '' for m in self.runtime.discovery() if m['topic'] == old_topic))

    def legacy_accessory_reset_fixture(self):
        from avionmesh_pi.lifecycle import reload_inventory
        inventory = self.store.inventory()
        inventory['devices'][1]['product_id'] = 91
        self.store.set('inventory', inventory)
        reload_inventory(self.runtime)
        detail = self.store.journals()[0]['detail']
        detail['product_id'] = 91
        detail.pop('uuid_hash')  # Older valid Accessory claims did not store it.
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), self.claim))
        setup = self.store.begin('accessory_setup', 40002, {
            'claim': self.claim, 'assignment_attempted': True,
            'original_assignment': 'unknown', 'reservation': inventory['groups'][1]})
        self.store.transition(setup, 'prepared', 'attempted')
        self.store.transition(setup, 'attempted', 'uncertain')
        return setup

    async def test_legacy_accessory_reset_revalidates_claim_after_exact_uuid_scan(self):
        setup = self.legacy_accessory_reset_fixture()
        before = copy.deepcopy(self.store.inventory())
        async def changed_claim():
            detail = self.store.journals()[0]['detail']
            detail['uuid'] = 'ff' * 16
            self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), self.claim))
            return [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        self.scan.discover = changed_claim
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile_reset(setup, True)
        self.assertEqual(self.store.inventory(), before)
        self.assertNotIn(40002, self.store.get('retired_addresses', []))
        self.assertEqual(self.store.journals()[-1]['status'], 'prepared')
        self.assertEqual(self.transport.frames, [])

    async def test_old_accessory_reset_receipt_without_claim_snapshot_remains_compatible(self):
        setup = self.legacy_accessory_reset_fixture()
        old_claim = copy.deepcopy(self.store.journals()[0])
        result = await self.lifecycle.reconcile_reset(setup, True)
        self.assertFalse(result['removed'])
        receipt = self.store.journals()[-1]
        receipt['detail'].pop('claim_receipt')
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                             (json.dumps(receipt['detail']), receipt['id']))
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        self.assertTrue((await self.lifecycle.reconcile(receipt['id']))['removed'])
        self.assertEqual(self.store.journals()[0], old_claim)
        self.assertNotIn('uuid_hash', old_claim['detail'])
        self.assertEqual(self.transport.frames, [])
        self.assertIn(40002, self.store.get('retired_addresses'))

    async def test_legacy_hash_exception_does_not_weaken_removal_or_accept_invalid_present_hash(self):
        setup = self.legacy_accessory_reset_fixture()
        with self.assertRaisesRegex(ValueError, 'Exact full UUID'):
            self.lifecycle.preview_remove(40002)
        claim = self.store.journals()[0]
        claim['detail']['uuid_hash'] = uuid_hash(bytes.fromhex(UUID)) + 1
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(claim['detail']), self.claim))
        with self.assertRaisesRegex(ValueError, 'Exact full UUID'):
            await self.lifecycle.reconcile_reset(setup, True)
        self.assertEqual(self.scan.calls, 0)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(len(self.store.journals()), 2)
