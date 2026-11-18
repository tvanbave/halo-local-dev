import asyncio
import tempfile
import unittest
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.mqtt import MqttService
from support import synthetic_store
from test_service import KEY,INVENTORY,FakeTransport

class SessionIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=synthetic_store(self.temp.name,KEY)
        self.store.import_inventory(INVENTORY);self.transport=FakeTransport()
        self.runtime=Runtime(self.store,KEY,self.transport,clock=lambda:10)
        self.runtime.mesh_ready=self.runtime.mqtt_ready=True;self.runtime.session()
        self.mqtt=MqttService(self.runtime,{'host':'unused.invalid'})
        self.mqtt.outbox.connection(True);self.runtime.sink=self.mqtt.outbox.put
    async def asyncTearDown(self):self.store.close();self.temp.cleanup()
    async def test_pre_boundary_queued_command_and_event_are_discarded(self):
        self.mqtt.ingest(self.runtime.core.prefix+'/light/40002/set',b'{"state":"ON"}',False)
        self.mqtt.outbox.put(self.runtime.core.intent(self.runtime.core.prefix+'/controller/40001/input/on',{'event_type':'on'}))
        self.runtime.mesh_ready=False;self.runtime.session();self.runtime.mesh_ready=True;self.runtime.session()
        task=asyncio.create_task(self.mqtt.command_loop())
        try:
            await asyncio.sleep(.01)
            self.assertEqual(self.transport.frames,[])
            self.assertEqual(len(self.mqtt.outbox.events),0)
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    async def test_compound_control_cannot_continue_on_replacement_session(self):
        original=self.transport.send
        async def replace(frame):
            await original(frame)
            self.runtime.mesh_ready=False;self.runtime.session();self.runtime.mesh_ready=True;self.runtime.session()
        self.transport.send=replace
        with self.assertRaises(RuntimeError):
            await self.runtime.control(40002,{'brightness':128,'color_temp':3500},confirm=False)
        self.assertEqual(len(self.transport.frames),1)

    async def test_bounds_and_malformed_mqtt_do_not_escape_parser(self):
        for i in range(200):self.mqtt.ingest(self.runtime.core.prefix+'/light/'+str(40000+i)+'/set',b'{"state":"ON"}',False)
        self.assertEqual(len(self.mqtt.commands),128)
        self.assertEqual(self.mqtt.counts['command_overflow'],72)
        self.mqtt.ingest(self.runtime.core.prefix+'/light/40002/set',b'['*1000+b']'*1000,False)
        self.assertEqual(self.mqtt.counts['invalid_command'],1)
        self.runtime.mesh_ready=False;self.runtime.session()
        self.mqtt.ingest(self.runtime.core.prefix+'/light/40002/set',b'{"state":"ON"}',False)
        self.assertEqual(len(self.mqtt.commands),0)
        self.assertEqual(self.mqtt.counts['offline_command'],1)

    async def test_native_future_cannot_accept_pre_session_evidence(self):
        from avionmesh_pi.accessory import AccessoryOnboarding
        from avionmesh_pi.protocol import encrypt_mcp
        identifier=self.store.begin('accessory_setup',40001,{'reservation':{'group_id':291},'product_id':127})
        setup=AccessoryOnboarding(self.runtime)
        task=asyncio.create_task(setup.observe_native(identifier,timeout=.2))
        await asyncio.sleep(0)
        frame=encrypt_mcp(b'\x00\x80\x73'+bytes.fromhex('000a012300ff00000000'),KEY,77,40001)
        self.runtime.observe(frame,10);self.runtime.session()
        with self.assertRaises(RuntimeError):await task
        self.assertEqual(self.runtime.waiters,[])

    async def test_full_history_and_largest_supported_inventory_fit_state_outbox(self):
        from avionmesh_pi.bridge import Bridge,load_devices
        data={'devices':[{'device_id':40000+i,'product_id':127,'name':'Keypad','exposed':True} for i in range(512)],'groups':[INVENTORY['groups'][1]]}
        self.store.import_inventory(data);self.runtime.core=Bridge(load_devices(data['devices']),KEY)
        # Stress the largest fully active inventory, not unverified slots whose
        # required trigger tombstones are covered by the discovery contract tests.
        for device in data['devices']:
            self.runtime.core.controllers.bindings[device['device_id']] = {'slots': [
                {'slot': slot, 'target': 300 + slot, 'flags': 8, 'level': 255}
                for slot in range(1, 5)]}
        messages=self.runtime.discovery()
        self.assertFalse(any(m['payload'] == '' for m in messages))
        topics=self.store.get('discovery_ledger')
        retired=['homeassistant/light/pi_lab_retired_'+str(i)+'/config' for i in range(32768-len(topics))]
        self.store.set('discovery_ledger',topics+retired)
        self.runtime.emit(self.runtime.discovery())
        self.assertEqual(self.mqtt.outbox.counts['state_overflow'],0)
        cleared=set()
        while (message:=self.mqtt.outbox.take()) is not None:
            if message['payload']=='':cleared.add(message['topic'])
        self.assertEqual(cleared,set(retired))
