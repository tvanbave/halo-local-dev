"""Retained dimmer destinations protect removal even without live observations."""
import copy
import json
import unittest

from avionmesh_pi.association import uuid_hash
from avionmesh_pi.lifecycle import reload_inventory
import test_lifecycle as lifecycle_tests


class DimmerRemovalDependencyTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = lifecycle_tests.LifecycleTests.asyncTearDown

    async def asyncSetUp(self):
        await lifecycle_tests.LifecycleTests.asyncSetUp(self)
        inventory = self.store.inventory()
        inventory['devices'][0]['product_id'] = 91
        self.store.set('inventory', inventory)
        reload_inventory(self.runtime)

    def receipt(self, identifier, group, *, owner=40001, status='verified', managed=True):
        detail = {'reservation': copy.deepcopy(group), 'product_id': 91,
                  'assignment_read_back': False}
        if managed:
            detail.update(control_schema='dimmer-control-v1',
                          mode='ha' if group['protected'] else 'native', assignment_attempted=True)
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                             (identifier, 'dimmer_edit', owner, status, json.dumps(detail)))
        return detail

    async def test_verified_managed_and_legacy_receipts_protect_last_light_after_restart(self):
        for managed in (True, False):
            with self.subTest(managed=managed):
                self.receipt('edit', self.store.inventory()['groups'][0], managed=managed)
                self.runtime.session()
                self.assertEqual(self.runtime.native_observations, {})
                self.assertEqual(self.store.get('target_sources', {}), {})
                preview = self.lifecycle.preview_remove(40002)
                self.assertIn('native_controller_group_would_be_empty', preview['blockers'])
                self.assertEqual(preview['native_dependencies'],
                    [{'controller': 40001, 'destination': 291, 'direct': False}])
                with self.assertRaisesRegex(ValueError, 'dependency'):
                    await self.lifecycle.remove(40002)
                self.assertEqual(self.transport.frames, [])
                self.store.db.execute("DELETE FROM journals WHERE id='edit'")

    async def test_new_ha_receipt_does_not_erase_conservative_native_history(self):
        self.receipt('native', self.store.inventory()['groups'][0])
        self.receipt('ha', self.store.inventory()['groups'][1])
        preview = self.lifecycle.preview_remove(40002)
        self.assertIn('native_controller_group_would_be_empty', preview['blockers'])
        self.assertEqual(preview['native_dependencies'],
                         [{'controller': 40001, 'destination': 291, 'direct': False}])

    async def test_cancelled_receipt_or_removed_controller_does_not_create_dependency(self):
        group = self.store.inventory()['groups'][0]
        self.receipt('cancelled', group, status='cancelled')
        self.receipt('absent', group, owner=40099)
        self.assertEqual(self.lifecycle.preview_remove(40002)['native_dependencies'], [])

    async def test_confirmed_controller_removal_retires_edits_and_preserves_reservations(self):
        inventory = self.store.inventory()
        inventory['devices'][1]['product_id'] = 91
        self.store.set('inventory', inventory)
        reload_inventory(self.runtime)
        claim = self.store.journals()[0]['detail']
        claim['product_id'] = 91
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(claim), self.claim))
        detail = self.receipt('edit', inventory['groups'][1], owner=40002)
        self.store.reserve_targets([292, 24575])
        result = await self.lifecycle.remove(40002)
        self.assertEqual(next(j for j in self.store.journals() if j['id'] == 'edit')['status'], 'verified')
        identity = lifecycle_tests.UUID
        self.scan.results = [{'uuid': identity, 'uuid_hash': uuid_hash(bytes.fromhex(identity))}]
        self.assertTrue((await self.lifecycle.reconcile(result['transaction']))['removed'])
        receipt = next(j for j in self.store.journals() if j['id'] == 'edit')
        self.assertEqual(receipt['status'], 'retired')
        self.assertEqual(receipt['detail'], detail)
        self.assertIn(inventory['groups'][1], self.store.inventory()['groups'])
        self.assertTrue({292, 24575}.issubset(self.store.get('known_targets')))
        self.assertEqual(len(self.transport.frames), 1)
