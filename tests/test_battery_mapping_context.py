"""Scoped semantic cache evidence; synthetic state only, never cache migration."""
import copy
import json
import unittest

from avionmesh_pi.battery_mapping import CACHE_KEY, CONTEXT_SCHEMA
from avionmesh_pi.lifecycle import reload_inventory
from avionmesh_pi.onboarding import table_from_rows
import test_battery_mapping_recovery as recovery_case


class ScopedMappingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery_case.BatteryMappingRecoveryTests.asyncSetUp
    asyncTearDown = recovery_case.BatteryMappingRecoveryTests.asyncTearDown
    sleep = recovery_case.BatteryMappingRecoveryTests.sleep
    save = recovery_case.BatteryMappingRecoveryTests.save
    reconnect = recovery_case.BatteryMappingRecoveryTests.reconnect
    prepared = recovery_case.BatteryMappingRecoveryTests.prepared
    restart = recovery_case.BatteryMappingRecoveryTests.restart
    press = recovery_case.BatteryMappingRecoveryTests.press
    activations = recovery_case.BatteryMappingRecoveryTests.activations

    def claim(self, target=40001, identity='0123456789abcdef0123456789abcdef'):
        identifier = self.store.begin('claim', target, {'uuid': identity, 'product_id': 126})
        self.store.transition(identifier, 'prepared', 'attempted')
        self.store.transition(identifier, 'attempted', 'verified')
        return identifier

    def assert_cached(self):
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'cached')

    async def test_own_and_other_names_rooms_and_inventory_order_survive_reload_and_restart(self):
        await self.prepared()
        context = self.store.get(CACHE_KEY)['40001']['context']
        data = self.store.inventory()
        for device in data['devices']:device['name'] = 'New label ' + str(device['device_id'])
        for group in data['groups']:
            if not group['protected']:group['name'] = 'New room label'
        data['devices'].reverse();data['groups'].reverse()
        self.store.import_inventory(data)
        self.store.set('device_rooms', {'40001': 'Living Room', '40002': 'Office'})
        reload_inventory(self.runtime)
        self.assert_cached()
        self.assertEqual(self.store.get(CACHE_KEY)['40001']['context'], context)
        self.restart();self.press()
        self.assertEqual(self.activations()[0]['button_mapping'], 'cached_table')
        self.transport.send.assert_not_awaited()

    async def test_unrelated_device_group_additions_and_removals_preserve_four_cached_inputs(self):
        await self.prepared()
        await self.save([{'slot': slot, 'mode': 'ha'} for slot in (2, 3, 4)])
        table = copy.deepcopy(self.runtime.core.controllers.bindings[40001])
        context = self.store.get(CACHE_KEY)['40001']['context']
        data = self.store.inventory()
        data['devices'].append({'device_id': 40003, 'product_id': 91, 'name': 'Added accessory', 'exposed': False})
        data['groups'].append({'group_id': 301, 'name': 'Added group', 'members': [40002], 'protected': False, 'exposed': False})
        self.store.import_inventory(data);reload_inventory(self.runtime);self.assert_cached()
        self.assertEqual(self.store.get(CACHE_KEY)['40001']['context'], context)
        data['devices'] = [d for d in data['devices'] if d['device_id'] != 40003]
        data['groups'] = [g for g in data['groups'] if g['group_id'] != 301]
        self.store.import_inventory(data);reload_inventory(self.runtime);self.assert_cached()
        self.restart()
        for index, row in enumerate(table['slots']):self.press(10000 + index, target=row['target'])
        self.assertEqual([e['button'] for e in self.activations()], [1, 2, 3, 4])
        self.assertTrue(all(e['button_mapping'] == 'cached_table' for e in self.activations()))
        self.transport.send.assert_not_awaited()

    async def test_unrelated_claim_retirement_and_pending_jobs_do_not_revoke(self):
        self.claim()
        await self.prepared()
        other = self.claim(40003, '11111111111111111111111111111111')
        self.store.set('retired_claims', [other])
        self.store.set('retired_addresses', [40003])
        self.store.begin('claim', 40004, {'uuid': '22222222222222222222222222222222', 'product_id': 91})
        self.restart();self.assert_cached();self.press()
        self.assertEqual(len(self.activations()), 1)
        self.transport.send.assert_not_awaited()

    async def test_owned_group_deletion_protection_members_sharing_and_name_changes_revoke(self):
        await self.prepared()
        original = self.store.inventory()
        for change in ('delete', 'protected', 'members', 'exposed', 'name'):
            with self.subTest(change=change):
                data = copy.deepcopy(original)
                group = next(g for g in data['groups'] if g['group_id'] == self.target)
                if change == 'delete':data['groups'].remove(group)
                elif change == 'protected':group['protected'] = False
                elif change == 'members':group['members'] = [40002]
                elif change == 'exposed':group['exposed'] = True
                else:group['name'] = 'Changed private reservation'
                self.store.set('inventory', data)  # Fault injection bypasses metadata admission.
                self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
                self.store.set('inventory', original)
                self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
                await self.editor.read(40001)  # Fresh evidence is the only recovery.
                self.assert_cached()

    async def test_competing_stale_table_reference_revokes(self):
        await self.prepared()
        rows = [bytes.fromhex(row['raw']) for row in self.table['slots']]
        self.store.save_binding(40003, table_from_rows(rows))
        self.store.invalidate_bindings()
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_state_mesh_or_relevant_claim_identity_changes_revoke(self):
        identifier = self.claim()
        await self.prepared()
        for key in ('state_id', 'mesh_fingerprint'):
            with self.subTest(key=key):
                original = self.store.get(key)
                self.store.set(key, 'different synthetic identity')
                self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
                self.store.set(key, original)
                self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
                await self.editor.read(40001)
        receipt = next(j for j in self.store.journals() if j['id'] == identifier)
        receipt['detail']['uuid'] = '33333333333333333333333333333333'
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(receipt['detail']), identifier))
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_same_uuid_at_other_address_is_a_relevant_competitor(self):
        self.claim();await self.prepared()
        self.claim(40003)
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
        await self.editor.read(40001)
        self.assertNotEqual(self.editor.battery_cache.status(40001)['status'], 'cached')

    async def test_same_uuid_retired_peer_remains_a_dependency(self):
        self.claim()
        peer = self.claim(40003)
        self.store.set('retired_claims', [peer])
        self.store.set('retired_addresses', [40003])
        await self.prepared()
        self.store.set('retired_addresses', [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
        self.store.set('retired_addresses', [40003])
        await self.editor.read(40001)
        self.assert_cached()
        self.store.set('retired_claims', [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_malformed_own_claim_identity_cannot_create_replacement_cache(self):
        identifier = self.claim();await self.prepared()
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                              (json.dumps({'product_id': 126, 'uuid': None}), identifier))
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
        await self.editor.read(40001)
        self.assertNotEqual(self.editor.battery_cache.status(40001)['status'], 'cached')

    async def test_own_claim_retirement_and_malformed_retirement_fail_closed(self):
        identifier = self.claim();await self.prepared()
        for key, value in (('retired_claims', [identifier]), ('retired_claims', {identifier: True}),
                           ('retired_claims', [None]), ('retired_addresses', ['40001'])):
            with self.subTest(key=key, value=value):
                self.store.set(key, value)
                self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
                self.store.set(key, [])
                self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
                await self.editor.read(40001)
                self.assert_cached()

    async def test_model_removal_and_table_change_cannot_keep_cache(self):
        await self.prepared()
        original = self.store.inventory()
        data = copy.deepcopy(original);data['devices'][0]['product_id'] = 127
        self.store.set('inventory', data)
        self.assertIsNone(self.editor.battery_cache.mapping(40001))
        self.store.set('inventory', original)
        self.assertIsNone(self.editor.battery_cache.mapping(40001))
        await self.editor.read(40001)
        rows = [bytes.fromhex(row['raw']) for row in self.table['slots']]
        rows[1] = bytes.fromhex('01300002012d08ff')
        self.store.save_binding(40001, table_from_rows(rows))
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_legacy_or_unknown_context_requires_explicit_read_and_never_migrates(self):
        await self.prepared()
        for schema in (None, 'future-schema'):
            with self.subTest(schema=schema):
                records = self.store.get(CACHE_KEY)
                if schema is None:records['40001']['context'].pop('schema')
                else:records['40001']['context']['schema'] = schema
                self.store.set(CACHE_KEY, records)
                self.assertEqual(self.editor.battery_cache.status(40001)['reason'], 'cache_schema_requires_fresh_readback')
                self.assertEqual(self.store.get(CACHE_KEY)['40001']['state'], 'revoked')
                await self.editor.read(40001)
                self.assertEqual(self.store.get(CACHE_KEY)['40001']['context']['schema'], CONTEXT_SCHEMA)
                self.assert_cached()

    async def test_existing_revocation_cannot_be_resurrected_by_cosmetic_changes_or_restart(self):
        await self.prepared()
        self.editor.battery_cache.revoke(40001, 'identity_inventory_or_receipts_changed')
        data = self.store.inventory();data['devices'][0]['name'] = 'New cosmetic label'
        self.store.import_inventory(data);self.store.set('device_rooms', {'40001': 'Room'})
        self.restart();self.press()
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
        self.assertEqual(self.activations(), [])
        self.transport.send.assert_not_awaited()


if __name__ == '__main__':unittest.main()
