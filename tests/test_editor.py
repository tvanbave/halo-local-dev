import tempfile
import unittest

from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY, INVENTORY


class EditorTransport:
    ready = True
    dropped = 0
    def __init__(self):
        self.runtime = None
        self.sequence = 1
        self.rows = [bytes.fromhex("01300001012308ff")] + [bytes([1,48,0,i,255,255,0,0]) for i in range(2,5)]
        self.pending = None
        self.writes = []
        self.fail_after_secondary = False
        self.failed = False
    async def send(self, frame):
        packet = decrypt_mcp(frame, KEY)
        p = packet.payload
        if p[1] != 48:
            raise AssertionError("Unexpected operation")
        if p[0] in (1,4):
            if self.failed:
                raise RuntimeError("Injected readback loss")
            count = sum(row[4:] != b"\xff\xff\0\0" for row in self.rows)
            response = bytes([4,48,0,count,4]) if p[0] == 4 else self.rows[p[5]-1]
        else:
            self.writes.append(p)
            verb, token, slot = p[0], p[4], p[5]
            if verb == 5:
                self.rows[slot-1] = bytes([1,48,0,slot,255,255,0,0])
                code = 1
            elif slot & 0x20:
                assert self.pending is not None
                self.rows[(slot & 0x1f)-1] = self.pending
                self.pending = None
                code = 1
                if self.fail_after_secondary:
                    self.failed = True
            else:
                self.pending = bytes([1,48,0,slot]) + p[6:10]
                code = 5
            response = bytes([verb,48,token,code])
        self.runtime.observe(encrypt_mcp(b"\x00\x80\x73" + response, KEY, self.sequence, packet.mcp_address), self.runtime.clock())
        self.sequence += 1
    async def disconnect(self):
        self.ready = False


class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.transport = EditorTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        self.editor = KeypadService(self.runtime)
        self.baseline = await self.editor.read(40001)
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_disabled_editor_never_programs(self):
        with self.assertRaises(ValueError):
            await self.editor.apply(40001, 1, "native", 291, 200, self.baseline['revision'])
        self.assertEqual(self.transport.writes, [])

    async def test_native_preset_edit_preserves_other_slots_and_verifies(self):
        result = await self.editor.apply(40001, 1, "native", 291, 200,
                                          self.baseline['revision'], allowed_models=(127,))
        self.assertTrue(result['verified'])
        self.assertEqual(len(self.transport.writes), 3)
        self.assertEqual(self.transport.rows[0].hex(), '01300001012308c8')
        self.assertEqual(self.store.journals()[0]['status'], 'verified')
        self.assertEqual([r.hex() for r in self.transport.rows[1:]], [s['raw'] for s in self.baseline['slots'][1:]])

    async def test_uncertain_write_reconciles_without_resend(self):
        self.transport.fail_after_secondary = True
        with self.assertRaises(RuntimeError):
            await self.editor.apply(40001, 1, "native", 291, 200,
                                      self.baseline['revision'], allowed_models=(127,))
        journal = self.store.journals()[0]
        self.assertEqual(journal['status'], 'uncertain')
        self.assertFalse(self.transport.ready)
        self.transport.failed = False
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()
        result = await self.editor.reconcile(journal['id'])
        self.assertTrue(result['verified'])
        self.assertEqual(len(self.transport.writes), 3)

    async def test_stale_revision_and_protected_group_refuse(self):
        with self.assertRaises(ValueError):
            await self.editor.apply(40001, 1, "native", 291, 200, 'stale', allowed_models=(127,))
        with self.assertRaises(ValueError):
            await self.editor.apply(40001, 1, "native", 292, 200, self.baseline['revision'], allowed_models=(127,))
        self.assertEqual(self.transport.writes, [])

    async def test_unchanged_edit_is_noop(self):
        result = await self.editor.apply(40001, 1, "native", 291, 255,
                                          self.baseline['revision'], allowed_models=(127,))
        self.assertFalse(result['changed'])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])

    async def test_failed_reread_revokes_existing_mapping(self):
        self.transport.failed = True
        with self.assertRaises(RuntimeError):
            await self.editor.read(40001)
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertNotIn(40001, self.store.bindings())

    async def test_protected_original_assignment_cannot_be_overwritten(self):
        self.transport.rows[0] = bytes.fromhex('01300001012408ff')
        table = await self.editor.read(40001)
        with self.assertRaises(ValueError):
            await self.editor.apply(40001, 1, 'unassigned', None, None,
                                    table['revision'], allowed_models=(127,))
        self.assertEqual(self.transport.writes, [])

    async def test_failure_before_first_write_cancels_prepared_journal(self):
        self.store.set('table_token', 255)
        with self.assertRaises(RuntimeError):
            await self.editor.apply(40001, 1, 'native', 291, 200,
                                    self.baseline['revision'], allowed_models=(127,))
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals()[0]['status'], 'cancelled')
        self.assertEqual(self.store.journals(pending=True), [])

    async def test_verified_delete_resumes_only_never_attempted_insertion(self):
        original_token = self.editor._token
        calls = 0
        def fail_second_token():
            nonlocal calls
            calls += 1
            if calls == 2:raise RuntimeError('Injected interruption after verified delete')
            return original_token()
        self.editor._token = fail_second_token
        with self.assertRaises(RuntimeError):
            await self.editor.apply(40001,1,'native',291,200,self.baseline['revision'],allowed_models=(127,))
        journal = self.store.journals()[0]
        self.assertEqual(journal['detail']['stages'],['delete'])
        self.assertIn('delete_checkpoint',journal['detail'])
        self.editor._token = original_token
        self.runtime.mesh_ready = self.transport.ready = True
        self.runtime.session()
        result = await self.editor.resume_edit(journal['id'],allowed_models=(127,))
        self.assertTrue(result['verified'])
        self.assertEqual([p[0] for p in self.transport.writes],[5,2,2])
        self.assertEqual(self.store.journals()[0]['detail']['before'],self.baseline)

    async def test_attempted_insert_cannot_use_delete_checkpoint_to_retry(self):
        self.transport.fail_after_secondary = True
        with self.assertRaises(RuntimeError):
            await self.editor.apply(40001,1,'native',291,200,self.baseline['revision'],allowed_models=(127,))
        journal=self.store.journals()[0]
        self.runtime.mesh_ready=self.transport.ready=True
        with self.assertRaises(ValueError):
            await self.editor.resume_edit(journal['id'],allowed_models=(127,))
        self.assertEqual(len(self.transport.writes),3)
