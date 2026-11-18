import tempfile
import unittest

from avionmesh_pi.onboarding import KeypadOnboarding
from avionmesh_pi.protocol import decrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY, INVENTORY
from test_editor import EditorTransport

class SetupTransport(EditorTransport):
    def __init__(self):
        super().__init__()
        self.fail_primary = False
    async def send(self, frame):
        p = decrypt_mcp(frame,KEY).payload
        if p[:2] == b'\x02\x30':
            journal = self.runtime.store.journals()[-1]
            assert journal['status'] == 'attempted'
            assert journal['detail']['attempted']
            assert journal['detail']['reservations'][0] in self.runtime.store.inventory()['groups']
        await super().send(frame)
        if self.fail_primary and p[:2] == b'\x02\x30' and not p[5] & 0x20:
            raise RuntimeError('Injected primary acknowledgement/transport ambiguity')

class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name,KEY)
        self.store.import_inventory(INVENTORY)
        self.transport = SetupTransport()
        self.runtime = Runtime(self.store,KEY,self.transport,clock=lambda:10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities = {'wired_onboarding':True,'inventory_complete':True}
        self.runtime.session()
        self.setup = KeypadOnboarding(self.runtime)
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_only_unused_slots_are_filled_and_targets_reserved(self):
        original = self.transport.rows[0]
        result = await self.setup.setup(40001)
        self.assertTrue(result['ready'])
        self.assertEqual(self.transport.rows[0],original)
        self.assertEqual(len(self.transport.writes),6)
        groups = self.store.journals()[-1]['detail']['reservations']
        self.assertEqual([g['group_id'] for g in groups],[24575,24574,24573])
        self.assertTrue(all(g['protected'] and not g['exposed'] and not g['members'] for g in groups))
        self.assertEqual(self.store.journals()[-1]['status'],'verified')

    async def test_lost_readback_resumes_only_remaining_unattempted_slots(self):
        self.transport.fail_after_secondary = True
        with self.assertRaises(RuntimeError):
            await self.setup.setup(40001)
        journal = self.store.journals()[-1]
        self.assertEqual(journal['status'],'uncertain')
        self.transport.fail_after_secondary = self.transport.failed = False
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()
        result = await self.setup.resume(journal['id'])
        self.assertTrue(result['ready'])
        self.assertEqual(len(self.transport.writes),6)
        self.assertEqual(self.store.journals()[-1]['detail']['attempted'],[2,3,4])

    async def test_primary_only_outcome_never_repeats_or_completes_blindly(self):
        self.transport.fail_primary = True
        with self.assertRaises(RuntimeError):
            await self.setup.setup(40001)
        journal = self.store.journals()[-1]
        writes = len(self.transport.writes)
        self.transport.fail_primary = False
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()
        with self.assertRaises(ValueError):
            await self.setup.resume(journal['id'])
        self.assertEqual(len(self.transport.writes),writes)
        self.assertEqual(self.store.journals()[-1]['status'],'uncertain')

    async def test_unknown_occupied_record_preserved_and_not_ready(self):
        original = self.transport.rows[0] = bytes.fromhex('01300001012304a0')
        result = await self.setup.setup(40001)
        self.assertFalse(result['ready'])
        self.assertEqual(self.transport.rows[0],original)
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])

    async def test_disabled_or_incomplete_inventory_never_writes(self):
        self.runtime.capabilities['inventory_complete'] = False
        with self.assertRaises(ValueError):
            await self.setup.setup(40001)
        self.assertEqual(self.transport.writes,[])
        self.assertEqual(self.store.journals(),[])

    async def test_complete_native_table_is_exposed_without_programming(self):
        data = self.store.inventory()
        data['devices'][0]['exposed'] = False
        self.store.set('inventory',data)
        self.transport.rows = [bytes([1,48,0,i,1,35+i,8,255]) for i in range(1,5)]
        result = await self.setup.setup(40001)
        self.assertTrue(result['ready'])
        self.assertEqual(self.transport.writes,[])
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])

    async def test_native_evidence_and_stale_snapshots_reserve_allocator_targets(self):
        from avionmesh_pi.protocol import encrypt_mcp
        data = self.store.inventory()
        data['devices'].append({'device_id':40003,'product_id':91,'name':'Dimmer','exposed':False})
        self.store.import_inventory(data)
        from avionmesh_pi.lifecycle import reload_inventory
        reload_inventory(self.runtime)
        self.runtime.observe(encrypt_mcp(b'\0\x80\x73\0\x0a\x5f\xff\0\xff\0\0\0\0',KEY,100,40003),10)
        result = await self.setup.setup(40001)
        self.assertTrue(result['ready'])
        groups = self.store.journals()[-1]['detail']['reservations']
        self.assertNotIn(24575,[g['group_id'] for g in groups])
        self.store.close()
        self.store = Store(self.temp.name,KEY)
        self.assertIn(24575,self.store.get('known_targets'))

    async def test_persistence_failure_still_isolates_transport(self):
        import sqlite3
        original = self.setup.save
        def fail_on_attempt(identifier,detail,status):
            if status in ('attempted','uncertain'):
                raise sqlite3.OperationalError('Synthetic unavailable disk')
            original(identifier,detail,status)
        self.setup.save = fail_on_attempt
        with self.assertRaises(sqlite3.OperationalError):
            await self.setup.setup(40001)
        self.assertFalse(self.transport.ready)
        self.assertFalse(self.runtime.mesh_ready)
        self.assertEqual(self.transport.writes,[])
