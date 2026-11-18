import asyncio
import tempfile
import unittest
from avionmesh_pi.dimmer import DimmerEditor
from avionmesh_pi.protocol import encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_accessory import AccessoryTransport
from test_service import KEY

class DimmerTransport(AccessoryTransport):
    def emit(self,payload):
        # Model81 uses compact native commands, unlike Accessory's shifted layout.
        if len(payload)==10 and payload[:2]==b'\0\x0a':
            payload = payload[:4]+b'\0'+payload[6:7]+bytes(4)
        super().emit(payload)

class DimmerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name,KEY)
        self.store.import_inventory({'devices':[{'device_id':40001,'product_id':81,'name':'Synthetic Anyplace','exposed':True}],
             'groups':[{'group_id':291,'name':'Native destination','members':[],'exposed':False,'protected':False}]})
        self.transport = DimmerTransport()
        self.runtime = Runtime(self.store,KEY,self.transport,clock=lambda:10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities['anyplace_editor'] = True
        self.runtime.session()
        self.editor = DimmerEditor(self.runtime)
        self.transport.emit(b'\0\x0a\x01\x25\0\x80\0\0\0\0')
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()
    async def test_wake_scoped_write_needs_explicit_behavior_acceptance(self):
        # Adapt synthetic peer's receipt assertion to the dimmer journal shape.
        async def send(frame):
            from avionmesh_pi.protocol import decrypt_mcp
            self.transport.frames.append(frame)
            p = decrypt_mcp(frame,KEY).payload
            self.assertEqual(self.store.journals()[-1]['status'],'attempted')
            self.assertEqual(p[:2],b'\0\x1b')
            self.transport.emit(b'\0\x0a'+p[5:7]+b'\0\xff\0\0\0\0')
        self.transport.send = send
        pending = asyncio.create_task(self.editor.assign(40001,291))
        await asyncio.sleep(0)
        self.assertEqual(self.transport.frames,[])
        self.transport.emit(b'\x0b\x1c'+bytes(5))
        result = await pending
        self.assertTrue(result['acceptance_required'])
        self.assertEqual(self.store.journals()[-1]['status'],'uncertain')
        self.assertTrue(self.editor.accept_observed(result['transaction'])['accepted_observed_behavior'])
        self.assertEqual(self.store.journals()[-1]['status'],'verified')
        self.assertEqual(len(self.transport.frames),1)
    async def test_no_wake_cancels_without_programming(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.editor.assign(40001,291,timeout=.01)
        self.assertEqual(self.transport.frames,[])
        self.assertEqual(self.store.journals()[-1]['status'],'cancelled')
    async def test_no_baseline_refuses_without_programming(self):
        self.runtime.session()
        with self.assertRaises(ValueError):
            await self.editor.assign(40001,291)
        self.assertEqual(self.store.journals(),[])

    async def test_broadcast_native_command_is_not_assignment_baseline(self):
        self.runtime.session()
        self.transport.emit(b'\0\x0a\0\0\0\xff\0\0\0\0')
        with self.assertRaises(ValueError):
            await self.editor.assign(40001,291)
        self.assertEqual(self.transport.frames,[])

    async def test_reviewed_baseline_mismatch_or_malformed_refuses_before_journal(self):
        event, when = self.runtime.native_observations[40001]
        expected = {'target': event['target'], 'observed_ms': when, 'target_generation': event['target_generation']}
        for bad in (dict(expected, target=300), dict(expected, observed_ms=when+1),
                    dict(expected, target_generation=event['target_generation']+1),
                    dict(expected, observed_ms=True), dict(expected, extra=1), {}):
            with self.subTest(baseline=bad):
                with self.assertRaises(ValueError):
                    await self.editor.assign(40001, 291, expected_baseline=bad)
                self.assertEqual(self.store.journals(), [])
        self.assertEqual(self.transport.frames, [])

    async def test_future_dated_baseline_refuses_before_journal(self):
        event, when = self.runtime.native_observations[40001]
        self.runtime.native_observations[40001] = (event, when+1)
        with self.assertRaises(ValueError):
            await self.editor.assign(40001, 291)
        self.assertEqual(self.store.journals(), [])
        self.assertEqual(self.transport.frames, [])
