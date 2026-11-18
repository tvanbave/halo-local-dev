"""Synthetic browser-to-archive-to-replacement checks; only loopback HTTP."""
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from avionmesh_pi import backup_manager as b, quickstart as q, server
from avionmesh_pi.browser_auth import basic_header
from avionmesh_pi.installation_backup import decode_backup
from avionmesh_pi.mqtt_settings import MqttSettings
from avionmesh_pi.protocol import derive_mcp_key, decrypt_mcp, plan_light_command
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.service import private_text, load_config
from avionmesh_pi.storage import Store
from test_backup_manager import SyntheticInstallation, PASSWORD, HOST, PORT, metadata


async def chunks(raw):
    for start in range(0,len(raw),8192):
        yield raw[start:start+8192]
        await asyncio.sleep(0)


def confirmations(preview):
    return dict(preview_id=preview['preview_id'], revision=preview['revision'],
        confirm_old_bridge_stopped=True, confirm_replace=True, confirm_backup_history=True)


class BackupHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.fixture=SyntheticInstallation(self.root/'source')
        self.restarts=[]
        self.manager=b.BackupManager(self.fixture.runtime,self.fixture.config,self.restarts.append)
        self.fixture.runtime.backup_manager=self.manager
        self.token=self.fixture.token
        self.headers={'Authorization':'Bearer '+self.token}
        self.clients=[]
        self.client=await self.open_client(server.create_app(self.fixture.runtime,self.token))

    async def open_client(self,app):
        client=TestClient(TestServer(app));await client.start_server();self.clients.append(client);return client

    async def asyncTearDown(self):
        for client in reversed(self.clients):await client.close()
        self.manager.clear();await self.fixture.close();self.temp.cleanup()

    async def create(self):
        response=await self.client.post('/api/backup/create',headers=self.headers,json={'password':PASSWORD})
        self.assertEqual(response.status,200,await response.text())
        return await response.json()

    async def preview(self,result):
        response=await self.client.post('/api/backup/preview',headers=self.headers,
            json={'password':PASSWORD,'archive':result['archive']})
        self.assertEqual(response.status,200,await response.text());return await response.json()

    async def test_auth_origin_and_content_type_protect_every_backup_action(self):
        before=b.snapshot_db(self.fixture.store.db)
        for action in ('create','preview','restore'):
            for headers,expected in (({},401),(dict(self.headers,Origin='https://foreign.invalid'),403)):
                response=await self.client.post('/api/backup/'+action,headers=headers,json={})
                self.assertEqual(response.status,expected)
        self.assertEqual((await self.client.get('/api/backup/status')).status,401)
        response=await self.client.post('/api/backup/create',headers=self.headers,data='{}')
        self.assertEqual(response.status,415)
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        self.assertIsNone(self.manager.review)
        self.assertEqual(self.restarts,[]);self.assertEqual(self.fixture.transport.frames,[])

    async def test_browser_login_and_trusted_lan_backup_guards(self):
        for login in (False,True):
            kwargs={'browser_login':True} if login else {'trusted_lan_no_auth':True}
            client=await self.open_client(server.create_app(self.fixture.runtime,self.token,allowed_host=HOST+':'+str(PORT),**kwargs))
            headers={'Host':HOST+':'+str(PORT),'Origin':'http://'+HOST+':'+str(PORT)}
            if login:
                self.assertEqual((await client.get('/api/backup/status',headers=headers)).status,401)
                headers['Authorization']=basic_header('halo',self.token)
            status = await client.get('/api/backup/status', headers=headers)
            self.assertEqual(status.status, 200)
            self.assertEqual((await status.json())['owner_password_required'], not login)
            if not login:
                for action in ('create', 'preview', 'restore'):
                    for owner in ({}, {'Authorization':'Bearer wrong'}):
                        denied = await client.post('/api/backup/' + action, headers=dict(headers, **owner), json={})
                        self.assertEqual(denied.status, 403)
                        self.assertNotIn('WWW-Authenticate', denied.headers)
                        self.assertEqual((await denied.json())['error'], 'backup_owner_required')
                self.assertEqual(self.restarts, [])
                headers['Authorization'] = basic_header('halo', self.token)
            created = await client.post('/api/backup/create',headers=headers,json={'password':PASSWORD})
            self.assertEqual(created.status, 200)
            for extra in ({'Host':'foreign.invalid'},{'Origin':'https://foreign.invalid'},{'Sec-Fetch-Site':'cross-site'}):
                response=await client.post('/api/backup/create',headers=dict(headers,**extra),json={'password':PASSWORD})
                self.assertEqual(response.status,403)

    async def test_expired_review_and_consumed_restore_never_apply_twice(self):
        result=await self.create();preview=await self.preview(result)
        original_clock=self.manager.clock
        self.manager.clock=lambda:self.manager.review['expires']+1
        response=await self.client.post('/api/backup/restore',headers=self.headers,json=confirmations(preview))
        self.assertEqual(response.status,409);self.assertEqual((await response.json())['error'],'preview_expired')
        self.assertEqual(self.restarts,[])
        self.manager.clock=original_clock;preview=await self.preview(result)
        before=b.snapshot_db(self.fixture.store.db)
        response=await self.client.post('/api/backup/restore',headers=self.headers,json=confirmations(preview))
        self.assertEqual(response.status,200);self.assertTrue((await response.json())['accepted'])
        self.assertEqual(len(self.restarts),1)
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        for path in ('/api/backup/restore','/api/control','/api/metadata'):
            response=await self.client.post(path,headers=self.headers,json=confirmations(preview))
            self.assertEqual(response.status,409);self.assertEqual((await response.json())['error'],'restore_in_progress')
        self.assertEqual(len(self.restarts),1);self.assertEqual(self.fixture.transport.frames,[])

    async def test_confirmation_types_cannot_be_bypassed_by_truthy_values(self):
        preview=await self.preview(await self.create());body=confirmations(preview)
        for field in ('confirm_old_bridge_stopped','confirm_replace','confirm_backup_history'):
            for value in (False,1,'true',None):
                response=await self.client.post('/api/backup/restore',headers=self.headers,json=dict(body,**{field:value}))
                self.assertEqual(response.status,400)
        self.assertEqual(self.restarts,[])

    async def test_chunked_backup_over_normal_api_limit_can_preview_but_other_routes_cannot(self):
        self.fixture.store.set('synthetic_large_metadata', 'x'*(300*1024))
        result=await self.create()
        raw=json.dumps({'password':PASSWORD,'archive':result['archive']}).encode()
        self.assertGreater(len(raw),256*1024)
        response=await self.client.post('/api/backup/preview',headers=dict(self.headers,**{'Content-Type':'application/json'}),data=chunks(raw))
        self.assertEqual(response.status,200,await response.text());self.assertTrue((await response.json())['can_restore'])
        for data in (raw,chunks(raw)):
            response=await self.client.post('/api/backup/create',headers=dict(self.headers,**{'Content-Type':'application/json'}),data=data)
            self.assertEqual(response.status,413)

    async def test_chunked_upload_limit_enforced_before_preview_handler(self):
        with patch.object(server,'MAX_HTTP_BYTES',65536):
            client=await self.open_client(server.create_app(self.fixture.runtime,self.token))
        for data in (b' '*70000,chunks(b' '*70000)):
            with patch.object(self.manager,'preview',new=AsyncMock(side_effect=AssertionError('Do not decrypt oversized input'))):
                response=await client.post('/api/backup/preview',headers=dict(self.headers,**{'Content-Type':'application/json'}),data=data)
                self.assertEqual(response.status,413)
        self.assertIsNone(self.manager.review)

    async def test_full_http_replacement_restores_pairings_scenes_and_ha_identity(self):
        original=self.fixture.store.inventory();old_discovery=self.fixture.runtime.discovery()
        result=await self.create();archive=base64.b64decode(result['archive']);decoded=decode_backup(archive,PASSWORD)
        destination=self.root/'replacement'
        ready=asyncio.Event()
        with q.installation_lock(destination):
            token=q.password(destination)
            client=await self.open_client(q.setup_app(destination,'192.168.50.21',PORT,'hci1',token,ready,browser_login=True))
            headers={'Host':'192.168.50.21:'+str(PORT),'Origin':'http://192.168.50.21:'+str(PORT),'Authorization':basic_header('halo',token)}
            body={'password':PASSWORD,'archive':result['archive']}
            self.assertEqual((await client.post('/api/backup/preview',headers={'Host':headers['Host']},json=body)).status,401)
            response=await client.post('/api/backup/preview',headers=headers,json=body)
            self.assertEqual(response.status,200,await response.text());preview=await response.json()
            self.assertTrue(preview['can_restore']);self.assertEqual(preview['mode'],'replacement')
            self.assertFalse((destination/'bridge').exists());self.assertFalse(ready.is_set())
            response=await client.post('/api/backup/restore',headers=headers,json=confirmations(preview))
            self.assertEqual(response.status,200,await response.text());self.assertTrue((await response.json())['accepted'])
            self.assertTrue(ready.is_set())
            config=q.existing_config(destination,'192.168.50.21',PORT,'hci1',browser_login=True)
            self.assertEqual(config['node'],self.fixture.config['node'])
            self.assertEqual(config['adapter'],'hci1');self.assertEqual(config['http_host'],'192.168.50.21')
            self.assertEqual(config['api_token_file'],str(destination/'ui-password'))
            self.assertEqual(private_text(config['mesh_passphrase_file']),private_text(self.fixture.config['mesh_passphrase_file']))
            self.assertEqual(config['api_token_file'],str(destination/'ui-password'))
            saved=metadata(Path(config['state_directory'])/'bridge.sqlite3')
            self.assertEqual(saved['inventory'],original)
            self.assertEqual(saved['device_rooms'],self.fixture.store.get('device_rooms'))
            self.assertEqual(saved['native_scenes_v1'],self.fixture.store.get('native_scenes_v1'))
            store=Store(config['state_directory'],self.fixture.key)
            class Peer:
                ready=True
                def __init__(self):self.frames=[]
                async def send(self,frame):self.frames.append(frame)
            peer=Peer();runtime=Runtime(store,self.fixture.key,peer,node=config['node']);runtime.mesh_ready=True
            settings=MqttSettings(runtime,config['state_directory'])
            try:
                self.assertEqual(settings._value['broker']['password'],'synthetic-mqtt-secret')
                restored_discovery=runtime.discovery()
                def identities(messages):
                    return sorted((m['topic'],m['payload']['unique_id']) for m in messages if m['topic'].endswith('/config') and isinstance(m['payload'],dict) and 'unique_id' in m['payload'])
                self.assertEqual(identities(restored_discovery),identities(old_discovery))
                self.assertEqual(runtime.scene_engine._require('s'*32)['owner'],self.fixture.scene_owner)
                await runtime.send(plan_light_command(40002,brightness=255))
                self.assertEqual(len(peer.frames),1)
                packet=decrypt_mcp(peer.frames[0],self.fixture.key)
                self.assertEqual(packet.mcp_address,40002)
                self.assertEqual(packet.sequence,saved['sequence']+1)
            finally:
                await settings.close();store.close()
            final=(destination/'bridge/state/bridge.sqlite3').read_bytes()
            response=await client.post('/api/backup/restore',headers=headers,json=confirmations(preview))
            self.assertGreaterEqual(response.status,400)
            self.assertEqual((destination/'bridge/state/bridge.sqlite3').read_bytes(),final)
        self.assertEqual(self.fixture.transport.frames,[])

    async def test_first_run_chunked_limits_and_origin_are_enforced(self):
        destination=self.root/'bounded';ready=asyncio.Event()
        with q.installation_lock(destination):
            token=q.password(destination)
            with patch.object(b,'MAX_HTTP_BYTES',65536):
                client=await self.open_client(q.setup_app(destination,HOST,PORT,'hci0',token,ready))
            headers={'Host':HOST+':'+str(PORT),'Content-Type':'application/json'}
            status = await client.get('/api/backup/status', headers=headers)
            self.assertTrue((await status.json())['owner_password_required'])
            for path in ('/api/backup/preview','/api/backup/restore'):
                denied = await client.post(path, headers=headers, json={})
                self.assertEqual((await denied.json())['error'], 'backup_owner_required')
                response=await client.post(path,headers=dict(headers,Origin='https://foreign.invalid'),json={})
                self.assertEqual(response.status,403)
                response=await client.post(path,headers=dict(headers,Authorization=basic_header('halo',token)),data=chunks(b' '*70000))
                self.assertEqual(response.status,413)
            self.assertFalse(ready.is_set());self.assertFalse((destination/'bridge').exists())
