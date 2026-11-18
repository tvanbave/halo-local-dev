"""Synthetic Anyplace receipt recovery; no reset or target setter is transmitted."""
import copy
import json
import tempfile
import unittest

from avionmesh_pi.association import uuid_hash
from avionmesh_pi.lifecycle import Lifecycle
from avionmesh_pi.management import cancel_unattempted, edit_metadata, recovery_advice, revision
from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_lifecycle import Scan
from test_service import FakeTransport, KEY


UUID = '0123456789abcdef0123456789abcdef'


class AnyplaceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [{'device_id': 40001, 'product_id': 81,
            'name': 'Synthetic Anyplace', 'exposed': False}], 'groups': []})
        self.claim = self.store.begin('claim', 40001, {'uuid': UUID,
            'uuid_hash': uuid_hash(bytes.fromhex(UUID)), 'product_id': 81})
        self.store.transition(self.claim, 'prepared', 'attempted')
        self.store.transition(self.claim, 'attempted', 'verified')
        self.reservation = {'group_id': 24575, 'name': 'Synthetic HA target',
                            'protected': True, 'members': [], 'exposed': False}
        inventory = self.store.inventory()
        inventory['groups'].append(self.reservation)
        self.setup = self.store.begin('anyplace_setup', 40001, {'product_id': 81,
            'claim': self.claim, 'reservation': self.reservation,
            'original_assignment': 'unknown', 'assignment_attempted': False},
            inventory=inventory, reserved_targets=[24575])
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        self.scan = Scan()
        self.lifecycle = Lifecycle(self.runtime, self.scan)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def receipt(self, identifier=None):
        return next(j for j in self.store.journals() if j['id'] == (identifier or self.setup))

    def update(self, identifier, *, status=None, **detail):
        journal = self.receipt(identifier)
        journal['detail'].update(detail)
        self.store.db.execute('UPDATE journals SET status=?,detail=? WHERE id=?',
            (status or journal['status'], json.dumps(journal['detail']), identifier))

    def attempted(self):
        self.update(self.setup, status='uncertain', assignment_attempted=True,
                    wake={'sequence': 23, 'received_ms': 10})

    def exact_unpaired(self):
        self.scan.results = [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]

    def assert_no_transmit(self):
        self.assertEqual(self.transport.frames, [])

    async def test_passive_reset_retires_identity_and_keeps_owned_target_burned(self):
        self.attempted()
        original = copy.deepcopy(self.receipt())
        before = copy.deepcopy(self.store.inventory())
        self.exact_unpaired()
        result = await self.lifecycle.reconcile_reset(self.setup, True)
        self.assertTrue(result['removed'])
        self.assertFalse(result['write_retried'])
        self.assertEqual(self.scan.calls, 1)
        self.assertEqual(self.store.inventory()['devices'], [])
        self.assertEqual(self.store.inventory()['groups'], [self.reservation])
        self.assertIn(24575, self.store.get('known_targets'))
        self.assertIn(self.claim, self.store.get('retired_claims'))
        self.assertIn(40001, self.store.get('retired_addresses'))
        self.assertEqual(self.receipt()['status'], 'retired')
        reset = self.receipt(result['transaction'])
        self.assertEqual(reset['detail']['original_receipt'], original)
        self.assertFalse(reset['detail']['reset_dispatched'])
        with self.assertRaises(ValueError):
            self.store.import_inventory(before)
        self.assert_no_transmit()

    async def test_owner_declaration_is_required_before_scan(self):
        self.attempted()
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile_reset(self.setup, False)
        self.assertEqual(self.scan.calls, 0)
        self.assertEqual(len(self.store.journals()), 2)
        self.assert_no_transmit()

    async def test_missing_or_hash_only_identity_proof_retains_quarantine(self):
        self.attempted()
        self.assertFalse((await self.lifecycle.reconcile_reset(self.setup, True))['removed'])
        self.scan.results = [{'uuid': 'ff' * 16, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]
        self.assertFalse((await self.lifecycle.reconcile_reset(self.setup, True))['removed'])
        self.assertEqual(self.scan.calls, 2)
        self.assertIn(40001, self.runtime.core.devices)
        self.assertEqual(self.receipt()['status'], 'uncertain')
        self.assertEqual(len(self.store.journals()), 3)
        self.assert_no_transmit()

    async def test_claim_or_model_changes_cannot_authorize_reset(self):
        self.attempted()
        original_claim = self.receipt(self.claim)
        for defect in ('retired', 'wrong_model', 'wrong_target', 'unverified', 'wrong_kind'):
            with self.subTest(defect=defect):
                self.store.set('retired_claims', [self.claim] if defect == 'retired' else [])
                altered = copy.deepcopy(original_claim)
                if defect == 'wrong_model': altered['detail']['product_id'] = 91
                if defect == 'wrong_target': altered['target'] = 40002
                if defect == 'unverified': altered['status'] = 'cancelled'
                if defect == 'wrong_kind': altered['kind'] = 'keypad_setup'
                self.store.db.execute('UPDATE journals SET kind=?,target=?,status=?,detail=? WHERE id=?',
                    (altered['kind'], altered['target'], altered['status'],
                     json.dumps(altered['detail']), self.claim))
                with self.assertRaises(ValueError):
                    await self.lifecycle.reconcile_reset(self.setup, True)
        self.assertEqual(self.scan.calls, 0)
        self.assertEqual(len(self.store.journals()), 2)
        self.assert_no_transmit()

    async def test_unattempted_or_wrong_model_setup_cannot_authorize_reset(self):
        self.update(self.setup, status='uncertain')
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile_reset(self.setup, True)
        self.update(self.setup, product_id=91, assignment_attempted=True)
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile_reset(self.setup, True)
        self.assertEqual(self.scan.calls, 0)
        self.assert_no_transmit()

    async def test_changed_protected_reservation_blocks_identity_retirement(self):
        self.attempted()
        inventory = self.store.inventory()
        inventory['groups'][0]['name'] = 'Changed reservation'
        self.store.set('inventory', inventory)
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile_reset(self.setup, True)
        self.assertEqual(self.scan.calls, 0)
        self.assert_no_transmit()

    async def test_claim_change_during_discovery_is_rechecked_before_retirement(self):
        self.attempted()
        self.exact_unpaired()
        discover = self.scan.discover

        async def changed():
            self.store.set('retired_claims', [self.claim])
            return await discover()

        self.scan.discover = changed
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile_reset(self.setup, True)
        self.assertEqual(self.receipt()['status'], 'uncertain')
        self.assertIn(40001, self.runtime.core.devices)
        self.assertEqual(self.store.journals()[-1]['status'], 'prepared')
        self.assert_no_transmit()

    async def test_changed_child_receipt_cannot_reuse_reset_authority(self):
        self.attempted()
        await self.lifecycle.reconcile_reset(self.setup, True)
        reset = self.store.journals()[-1]
        self.update(self.setup, observed={'sequence': 30})
        self.exact_unpaired()
        with self.assertRaises(ValueError):
            await self.lifecycle.reconcile(reset['id'])
        self.assertIn(40001, self.runtime.core.devices)
        self.assertEqual(self.receipt(reset['id'])['status'], 'prepared')
        self.assert_no_transmit()

    async def test_parent_reset_receipt_change_during_discovery_cannot_retire_identity(self):
        self.attempted()
        await self.lifecycle.reconcile_reset(self.setup, True)
        reset = self.store.journals()[-1]
        self.exact_unpaired()
        discover = self.scan.discover

        async def changed():
            self.update(reset['id'], uuid='ff' * 16)
            return await discover()

        self.scan.discover = changed
        with self.assertRaisesRegex(ValueError, 'Removal state changed'):
            await self.lifecycle.reconcile(reset['id'])
        self.assertEqual(self.receipt()['status'], 'uncertain')
        self.assertIn(40001, self.runtime.core.devices)
        self.assertEqual(self.receipt(reset['id'])['status'], 'prepared')
        self.assertNotIn(self.claim, self.store.get('retired_claims', []))
        self.assert_no_transmit()

    async def test_parent_reset_target_must_match_retained_child_identity(self):
        self.attempted()
        await self.lifecycle.reconcile_reset(self.setup, True)
        reset = self.store.journals()[-1]
        self.store.db.execute('UPDATE journals SET target=? WHERE id=?', (40002, reset['id']))
        self.exact_unpaired()
        with self.assertRaisesRegex(ValueError, 'retained identity'):
            await self.lifecycle.reconcile(reset['id'])
        self.assertEqual(self.receipt()['status'], 'uncertain')
        self.assertIn(40001, self.runtime.core.devices)
        self.assertNotIn(self.claim, self.store.get('retired_claims', []))
        self.assert_no_transmit()

    async def test_conflicting_group_source_does_not_require_replaying_a_setter(self):
        self.attempted()
        self.store.observe_target(24575, 40002)
        self.exact_unpaired()
        self.assertTrue((await self.lifecycle.reconcile_reset(self.setup, True))['removed'])
        self.assertEqual(self.store.inventory()['groups'], [self.reservation])
        self.assertFalse(self.store.owned_target_safe(24575, 40001))
        self.assert_no_transmit()

    async def test_cancel_unattempted_keeps_reservation_and_claim(self):
        self.assertEqual(recovery_advice(self.receipt())['actions'], ['cancel_unattempted'])
        self.assertTrue(cancel_unattempted(self.store, self.setup)['cancelled'])
        self.assertEqual(self.receipt()['status'], 'cancelled')
        self.assertTrue(self.receipt()['detail']['dispatch_not_started'])
        self.assertEqual(recovery_advice(self.receipt())['actions'], [])
        self.assertEqual(self.store.inventory()['groups'], [self.reservation])
        self.assertIn(24575, self.store.get('known_targets'))
        self.assertEqual(self.receipt(self.claim)['status'], 'verified')
        with self.assertRaises(ValueError):
            edit_metadata(self.store, {'operation': 'forget_empty_group',
                'target': 24575, 'revision': revision(self.store)})
        self.assert_no_transmit()

    async def test_attempted_or_inconsistent_receipt_cannot_cancel(self):
        self.update(self.setup, assignment_attempted=True)
        with self.assertRaises(ValueError):
            cancel_unattempted(self.store, self.setup)
        self.attempted()
        with self.assertRaises(ValueError):
            cancel_unattempted(self.store, self.setup)
        advice = recovery_advice(self.receipt())
        self.assertEqual(advice['actions'], ['reconcile', 'observe_owner_reset'])
        self.assertFalse(advice['retry_allowed'])
        self.assertFalse(advice['assignment_read_back'])
        self.assert_no_transmit()
