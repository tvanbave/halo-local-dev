"""Service lifecycle and whole MQTT/HTTP/Bluetooth replacement laboratory."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient,TestServer
from avionmesh_pi import backup_manager as b, quickstart as q, service
from avionmesh_pi.bluez import BlueZTransport
from avionmesh_pi.mqtt_settings import MqttSettings
from avionmesh_pi.protocol import decrypt_mcp
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.storage import Store
from test_backup_manager import SyntheticInstallation, PASSWORD, metadata
from test_backup_http import confirmations
import test_service_wire as wire
from test_service_wire import ISOLATED, DEPENDENCIES, eventually, stop_service_task


class RestoreServiceShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, fail_disconnect=False, block_disconnect=False):
        with tempfile.TemporaryDirectory() as directory:
            source=SyntheticInstallation(Path(directory).resolve()/'source')
            config=copy.deepcopy(source.config)
            config.update(first_device_bootstrap=False,automatic_bootstrap=False,automatic_relay=False,
                relay_address='02:00:00:00:00:01',trusted_lan_no_auth=False,browser_login=False,
                http_host='127.0.0.1',http_port=8088)
            config['capabilities'].update(observation_refresh=False,startup_light_read=False)
            await source.close()
            events=[];stores=[];runtimes=[]
            class Finished(Exception):pass
            class TrackedStore(Store):
                def __init__(self,*args,**kwargs):
                    super().__init__(*args,**kwargs);stores.append(self);self.number=len(stores)
                    events.append(('store_open',self.number))
                def close(self):
                    super().close();events.append(('store_closed',self.number))
            class TrackedRuntime(Runtime):
                def __init__(self,*args,**kwargs):
                    super().__init__(*args,**kwargs);runtimes.append(self);self.number=len(runtimes)
                async def bluetooth_loop(self):
                    try:
                        if self.number==2:
                            raise Finished()
                        await asyncio.Event().wait()
                    finally:events.append(('worker_stopped',self.number))
            class Settings(MqttSettings):
                async def run(self):await asyncio.Event().wait()
                async def close(self):
                    await super().close();events.append(('mqtt_closed',self.runtime.number))
            class Transport:
                ready=False
                def __init__(self,*args,**kwargs):self.number=len(stores)
                async def disconnect(self):
                    events.append(('bluetooth_disconnect',self.number))
                    if block_disconnect:await asyncio.Event().wait()
                    if fail_disconnect:raise RuntimeError('synthetic private transport failure')
                    events.append(('bluetooth_closed',self.number))
            class Manager:
                def __init__(self,runtime,config,restart):self.restart=restart
                def clear(self):pass
            class Runner:
                def __init__(self,*args,**kwargs):self.number=len(stores)
                async def setup(self):pass
                async def cleanup(self):events.append(('http_closed',self.number))
            class Site:
                def __init__(self,*args,**kwargs):pass
                async def start(self):
                    if len(runtimes)==1:
                        asyncio.get_running_loop().call_soon(runtimes[-1].backup_manager.restart,{'reviewed':'synthetic'})
            def finish(current,plan):
                self.assertEqual(plan,{'reviewed':'synthetic'})
                self.assertIsNone(stores[0].db)
                for kind in ('http_closed','worker_stopped','mqtt_closed','bluetooth_closed','store_closed'):
                    self.assertIn((kind,1),events)
                events.append(('install',1));return current
            with patch('avionmesh_pi.service.Store',TrackedStore),patch('avionmesh_pi.service.Runtime',TrackedRuntime), \
                 patch('avionmesh_pi.service.MqttSettings',Settings),patch('avionmesh_pi.service.BlueZTransport',Transport), \
                 patch('avionmesh_pi.backup_manager.BackupManager',Manager),patch('avionmesh_pi.backup_manager.finish_restore',side_effect=finish) as apply, \
                 patch('aiohttp.web.AppRunner',Runner),patch('aiohttp.web.TCPSite',Site), \
                 patch('avionmesh_pi.service.SHUTDOWN_TIMEOUTS',{'http':1,'workers':1,'mqtt':1,'bluetooth':.03}):
                if fail_disconnect or block_disconnect:
                    with self.assertLogs('avionmesh_pi.service',level='WARNING') as logs:
                        with self.assertRaisesRegex(RuntimeError,'shutdown could not be confirmed'):
                            await service.serve(config)
                    self.assertNotIn('private transport',str(logs.output))
                    apply.assert_not_called();self.assertEqual(len(runtimes),1)
                else:
                    with self.assertRaises(Finished):await service.serve(config)
                    apply.assert_called_once()
                    self.assertLess(events.index(('store_closed',1)),events.index(('install',1)))
                    self.assertLess(events.index(('install',1)),events.index(('store_open',2)))
            self.assertFalse((Path(config['state_directory'])/b.POINTER).exists())
            self.assertNotIn('installation_superseded_v1',metadata(Path(config['state_directory'])/'bridge.sqlite3'))

    async def test_restart_resolves_active_installation_before_opening_any_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            source=SyntheticInstallation(Path(directory).resolve()/'source')
            original=copy.deepcopy(source.config);plan=source.backup()
            await source.close()
            installed=b.finish_restore(original,plan)
            with patch('avionmesh_pi.service._serve_once',new=AsyncMock()) as run:
                await service.serve(original)
            run.assert_awaited_once()
            resumed=run.await_args.args[0]
            self.assertNotEqual(resumed['state_directory'],original['state_directory'])
            self.assertEqual(resumed['state_directory'],installed['state_directory'])
            self.assertEqual(resumed['node'],original['node'])
            self.assertEqual(metadata(Path(resumed['state_directory'])/'bridge.sqlite3')['state_id'],source.state_id)
            self.assertIn('installation_superseded_v1',metadata(Path(original['state_directory'])/'bridge.sqlite3'))

    async def test_all_owners_close_before_replacement_install_and_restart(self):
        await self.exercise()

    async def test_failed_bluetooth_disconnect_never_replaces_or_restarts_installation(self):
        await self.exercise(fail_disconnect=True)

    async def test_timed_out_bluetooth_disconnect_never_replaces_installation(self):
        await self.exercise(block_disconnect=True)


@unittest.skipUnless(ISOLATED and DEPENDENCIES,'Requires explicit network:none loopback laboratory')
class RestoredWholeServiceTests(unittest.IsolatedAsyncioTestCase):
    # Real aiohttp and MQTT socket service, fake BlueZ scanner/GATT only. The
    # fixture explicitly refuses any active non-loopback network interface.
    asyncSetUp=wire.WholeServiceTests.asyncSetUp

    async def test_fresh_setup_restore_controls_original_light_and_group_without_pairing(self):
        self.runtime.store.set('native_scenes_v1',[{'id':'restored-scene','reference':1,'name':'Restored scene',
            'owner':self.runtime.scene_engine._owner(),'members':[{'target':40002,'brightness':128,'kelvin':4000}],
            'exposed':True}])
        self.runtime.store.set('device_rooms',{'40001':'Lab','40002':'Lab'})
        before_inventory=copy.deepcopy(self.runtime.store.inventory());old_owner=self.runtime.scene_engine._owner()
        async with self.http.post('/api/backup/create',json={'password':PASSWORD}) as response:
            self.assertEqual(response.status,200);backup=await response.json()
        original_discovery=json.loads(self.broker.retained['homeassistant/light/wire_lab_40002/config'])
        await stop_service_task(self.task)
        self.assertTrue(all(not client.is_connected for client in self.relay.clients))
        self.assertEqual(self.relay.frames,[])
        destination=self.root/'replacement';ready=asyncio.Event()
        with q.installation_lock(destination):
            token=q.password(destination)
            setup=TestClient(TestServer(q.setup_app(destination,'192.168.50.21',18089,'hci1',token,ready)))
            await setup.start_server()
            try:
                headers={'Host':'192.168.50.21:18089','Origin':'http://192.168.50.21:18089','Authorization':'Bearer '+token}
                response=await setup.post('/api/backup/preview',headers=headers,json={'archive':backup['archive'],'password':PASSWORD})
                self.assertEqual(response.status,200,await response.text());review=await response.json();self.assertTrue(review['can_restore'])
                response=await setup.post('/api/backup/restore',headers=headers,json=confirmations(review))
                self.assertEqual(response.status,200,await response.text());self.assertTrue(ready.is_set())
            finally:await setup.close()
            restored=q.existing_config(destination,'192.168.50.21',18089,'hci1')
            self.assertEqual(restored['node'],'wire_lab')
            # Keep every network socket inside the isolated loopback laboratory.
            # Production destination remapping was exercised by the setup API.
            restored.update(http_host='127.0.0.1',http_port=0,trusted_lan_no_auth=False,browser_login=False)
            def automatic(*args,**kwargs):
                return BlueZTransport('00:11:22:33:44:55',scanner=self.relay,client_factory=self.relay.client)
            with patch('avionmesh_pi.bootstrap.AutoBlueZTransport',side_effect=automatic):
                task=asyncio.create_task(service.serve(restored))
                try:
                    await eventually(lambda:len(self.runtimes)==2 and self.runtimes[-1].mesh_ready and self.runtimes[-1].mqtt_ready)
                    runtime=self.runtimes[-1]
                    self.assertEqual(runtime.store.inventory(),before_inventory)
                    self.assertEqual(runtime.store.get('device_rooms'),{'40001':'Lab','40002':'Lab'})
                    self.assertEqual(runtime.scene_engine._require('restored-scene')['owner'],old_owner)
                    self.assertEqual(runtime.core.node,'wire_lab')
                    port=self.sites[-1]._server.sockets[0].getsockname()[1]
                    async with ClientSession(base_url='http://127.0.0.1:'+str(port),headers={'Authorization':'Bearer '+token}) as http:
                        await eventually(lambda:self.broker.connects>=2)
                        await eventually(lambda:'homeassistant/light/wire_lab_40002/config' in self.broker.retained)
                        discovery=json.loads(self.broker.retained['homeassistant/light/wire_lab_40002/config'])
                        self.assertEqual(discovery['unique_id'],original_discovery['unique_id'])
                        self.assertEqual(discovery['command_topic'],original_discovery['command_topic'])
                        async with http.post('/api/control',json={'target':40002,'values':{'brightness':117}}) as response:
                            self.assertEqual(response.status,200);self.assertTrue((await response.json())['confirmed'])
                        self.assertEqual(self.relay.brightness,117)
                        async with http.post('/api/control',json={'target':291,'values':{'brightness':0}}) as response:
                            self.assertEqual(response.status,200)
                        await eventually(lambda:self.relay.brightness==0)
                        await self.broker.publish(discovery['command_topic'],'{"brightness":121}')
                        await eventually(lambda:self.relay.brightness==121)
                        await eventually(lambda:self.prefix+'/light/40002/state' in self.broker.retained and json.loads(self.broker.retained[self.prefix+'/light/40002/state'])['brightness']==121)
                    packets=[decrypt_mcp(frame,self.key) for frame in self.relay.frames]
                    self.assertTrue(any(p.mcp_address==0 and p.payload[:4]==b'\0\x0a\x01\x23' for p in packets))
                    self.assertTrue(any(p.mcp_address==40002 and p.payload[:2]==b'\0\x0a' for p in packets))
                    self.assertTrue(all(p.opcode==115 and p.payload[0] in (0,1) and p.payload[1] in (10,29) for p in packets),
                        'Restore or control emitted an association/programming opcode')
                    self.assertEqual(runtime.store.journals(),[])
                finally:await stop_service_task(task)
