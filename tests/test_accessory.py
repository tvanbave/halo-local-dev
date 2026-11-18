import asyncio
import tempfile
import unittest

from avionmesh_pi.accessory import AccessoryOnboarding
from avionmesh_pi.protocol import decrypt_mcp,encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY

class AccessoryTransport:
    ready = True
    dropped = 0
    def __init__(self):
        self.runtime = None
        self.frames = []
        self.sequence = 1
        self.native = True
        self.group = None
    def emit(self,payload):
        self.runtime.observe(encrypt_mcp(b'\x00\x80\x73'+payload,KEY,self.sequence,40001),10)
        self.sequence += 1
    async def send(self,frame):
        self.frames.append(frame)
        packet = decrypt_mcp(frame,KEY)
        p = packet.payload
        if p[:2] == b'\x06\xff':
            self.emit(bytes([6,255,p[4],1,0,0,0]))
        elif p[:2] == b'\x00\x1b':
            receipt = self.runtime.store.journals()[-1]
            assert receipt['detail']['assignment_attempted']
            self.group = p[5:7]
            self.emit(bytes([0,27,p[4],0]))
            if self.native:
                self.emit(b'\x00\x0a'+self.group+b'\x01\x00\x80\0\0\0')
        else:
            raise AssertionError('Unexpected command')
    async def disconnect(self):
        self.ready = False

class AccessoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name,KEY)
        self.store.import_inventory({'devices':[{'device_id':40001,'product_id':91,'name':'Synthetic','exposed':False}],'groups':[]})
        j = self.store.begin('claim',40001,{'uuid':'01'*16,'product_id':91})
        self.store.transition(j,'prepared','attempted')
        self.store.transition(j,'attempted','verified')
        self.transport = AccessoryTransport()
        self.runtime = Runtime(self.store,KEY,self.transport,clock=lambda:10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities = {'accessory_onboarding':True,'inventory_complete':True}
        self.runtime.session()
        self.setup = AccessoryOnboarding(self.runtime)
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_fresh_ping_assignment_and_behavior_complete_without_readback_claim(self):
        result = await self.setup.setup(40001)
        self.assertTrue(result['native_behavior_observed'])
        self.assertFalse(result['assignment_read_back'])
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.assertTrue(self.store.inventory()['groups'][0]['protected'])
        self.assertEqual(len(self.transport.frames),2)
        with self.assertRaises(ValueError):
            await self.setup.setup(40001)

    async def test_ack_without_behavior_is_uncertain_then_observation_only_reconciles(self):
        self.transport.native = False
        with self.assertRaises(asyncio.TimeoutError):
            await self.setup.setup(40001,timeout=.01)
        j = self.store.journals()[-1]
        self.assertEqual(j['status'],'uncertain')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()
        task = asyncio.create_task(self.setup.reconcile(j['id'],timeout=1))
        await asyncio.sleep(0)
        self.transport.emit(b'\x00\x0a'+self.transport.group+b'\x01\x00\xff\0\0\0')
        self.assertTrue((await task)['ready'])
        self.assertEqual(len(self.transport.frames),2)

    async def test_missing_fresh_claim_cannot_enroll_imported_controller(self):
        self.store.db.execute('DELETE FROM journals')
        with self.assertRaises(ValueError):
            await self.setup.setup(40001)
        self.assertEqual(self.transport.frames,[])

    async def test_desired_then_conflicting_native_target_cannot_complete(self):
        original = self.transport.send
        async def contradictory(frame):
            await original(frame)
            if decrypt_mcp(frame,KEY).payload[:2]==b'\0\x1b':
                self.transport.emit(b'\0\x0a\x01\x23\x01\0\xff\0\0\0')
        self.transport.send = contradictory
        with self.assertRaises(RuntimeError):
            await self.setup.setup(40001)
        self.assertEqual(self.store.journals()[-1]['status'],'uncertain')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])

    async def test_reset_recovery_retires_identity_without_device_writes(self):
        from avionmesh_pi.lifecycle import Lifecycle
        from avionmesh_pi.association import uuid_hash
        self.transport.native = False
        with self.assertRaises(asyncio.TimeoutError):
            await self.setup.setup(40001,timeout=.01)
        setup = self.store.journals()[-1]
        self.runtime.mesh_ready = self.transport.ready = True
        identity = '01'*16
        class Scanner:
            async def discover(self):
                return [{'uuid':identity,'uuid_hash':uuid_hash(bytes.fromhex(identity))}]
        result = await Lifecycle(self.runtime,Scanner()).reconcile_reset(setup['id'],True)
        self.assertTrue(result['removed'])
        self.assertEqual(len(self.transport.frames),2)
        self.assertEqual(self.store.inventory()['devices'],[])
        self.assertEqual(self.store.journals()[-1]['detail']['original_receipt'],setup)
        self.assertFalse(self.store.journals()[-1]['detail']['reset_dispatched'])
        self.assertTrue(self.store.inventory()['groups'][0]['protected'])
        self.store.import_inventory(self.store.inventory())

    async def test_reset_owner_blocks_child_acceptance_and_rejects_changed_backup(self):
        from avionmesh_pi.lifecycle import Lifecycle
        from avionmesh_pi.association import uuid_hash
        self.transport.native = False
        with self.assertRaises(asyncio.TimeoutError):
            await self.setup.setup(40001,timeout=.01)
        original = self.store.journals()[-1]
        self.runtime.mesh_ready = self.transport.ready = True
        class Scanner:
            results = []
            async def discover(self):return self.results
        scanner = Scanner()
        lifecycle = Lifecycle(self.runtime,scanner)
        result = await lifecycle.reconcile_reset(original['id'],True)
        self.assertFalse(result['removed'])
        with self.assertRaises(RuntimeError):
            await self.setup.reconcile(original['id'],timeout=.01)
        reset = self.store.journals()[-1]
        # Simulate a contradictory persisted child update; reset must not consume it.
        self.store.db.execute("UPDATE journals SET status='verified' WHERE id=?",(original['id'],))
        scanner.results = [{'uuid':'01'*16,'uuid_hash':uuid_hash(bytes.fromhex('01'*16))}]
        with self.assertRaises(ValueError):
            await lifecycle.reconcile(reset['id'])
        self.assertEqual(len(self.store.inventory()['devices']),1)
        self.assertEqual(self.store.journals()[-1]['status'],'prepared')

    async def test_failed_uncertainty_save_still_disconnects(self):
        import sqlite3
        self.transport.native = False
        original_save = self.setup.save
        def save(identifier,detail,status):
            if status=='uncertain':raise sqlite3.OperationalError('Synthetic disk unavailable')
            return original_save(identifier,detail,status)
        self.setup.save = save
        with self.assertRaises(sqlite3.OperationalError):
            await self.setup.setup(40001,timeout=.01)
        self.assertFalse(self.transport.ready)
        self.assertFalse(self.runtime.mesh_ready)
        self.assertEqual(self.store.journals()[-1]['status'],'attempted')
