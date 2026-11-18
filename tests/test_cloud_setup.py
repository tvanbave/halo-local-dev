"""ESP-style account adoption with synthetic cloud replies, keys and transport."""
import asyncio
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from avionmesh_pi import quickstart as q
from avionmesh_pi.browser_auth import basic_header
from avionmesh_pi.cloud import CloudImport, parse_inventory
from avionmesh_pi.cloud_setup import CloudSetup, ImportSetupError
from avionmesh_pi.migration import admit_cloud_import, retire, status
from avionmesh_pi.protocol import derive_mcp_key, decrypt_mcp
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.service import load_config, automatic_relay_mode
from avionmesh_pi.storage import Store
from test_cloud import FakeCloud, Response, DEVICES, GROUP
from test_service import FakeTransport

PHRASE='synthetic-existing-cloud-key'
HOST='192.168.50.20'


def cloud():
    return FakeCloud({'sessions':{'credentials':{'auth_token':'synthetic-login-token'}},
        'user/locations':{'locations':[{'pid':'home','name':'Synthetic home'},{'pid':'other','name':'Other home'}]},
        'locations/home':{'location':{'passphrase':PHRASE}},
        'locations/home/abstract_devices':copy.deepcopy(DEVICES),
        'locations/home/groups':{'groups':[{'pid':'group_a','avid':291,'name':'Synthetic group'}]},
        'groups/group_a':{'group':{'devices':['device_a']}}})


class CloudSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http=cloud();self.now=100;self.setup=CloudSetup(self.http,clock=lambda:self.now)
    async def asyncTearDown(self):
        self.setup.cancel()
    async def login(self):
        result=await self.setup.login('owner@example.invalid','synthetic-password')
        self.session=result['session'];return result
    async def preview(self):
        await self.login();return await self.setup.preview(self.session,'home')
    async def test_multi_location_review_contains_inventory_but_no_secrets(self):
        login=await self.login()
        self.assertEqual(len(login['locations']),2);self.assertEqual(len(self.http.calls),2)
        preview=await self.setup.preview(self.session,'home')
        self.assertEqual(preview['inventory']['groups'][0]['members'],[40002])
        self.assertTrue(preview['local_control'])
        for secret in (PHRASE,'synthetic-login-token','synthetic-password'):
            self.assertNotIn(secret,json.dumps([login,preview]))
        saved=[]
        await self.setup.apply(self.session,preview['preview'],saved.append)
        self.assertEqual(saved[0]['passphrase'],PHRASE)
        self.assertIsNone(self.setup.token);self.assertIsNone(self.setup.plan)
        with self.assertRaises(ImportSetupError):
            await self.setup.apply(self.session,preview['preview'],saved.append)
        self.assertEqual(len(saved),1)
    async def test_unassigned_cloud_records_match_esp_skip_behavior(self):
        self.http.documents['locations/home/abstract_devices']['abstract_devices'].append(
            {'type':'device','pid':'unassigned','avid':0,'name':'Unassigned'})
        self.http.documents['locations/home/groups']['groups'].append({'pid':'unassigned_group','avid':0})
        preview=await self.preview()
        self.assertEqual(preview['skipped_unaddressed'],2)
        self.assertEqual(len(preview['inventory']['devices']),1)
        self.assertEqual(len(preview['inventory']['groups']),1)
        self.assertFalse(any('unassigned_group' in url for _,url,_ in self.http.calls))
    async def test_wrong_location_and_expiry_do_not_save(self):
        await self.login()
        with self.assertRaises(ImportSetupError):await self.setup.preview(self.session,'../sessions')
        self.assertEqual(len(self.http.calls),2)
        self.now+=601
        with self.assertRaises(ImportSetupError):await self.setup.preview(self.session,'home')
        self.assertIsNone(self.setup.token)
    async def test_redirect_credentials_and_malformed_inventory_do_not_leak(self):
        self.http.documents['sessions']=Response({'secret':'do-not-echo'},302)
        with self.assertRaisesRegex(ImportSetupError,'Could not load') as caught:await self.login()
        self.assertNotIn('do-not-echo',str(caught.exception));self.assertEqual(len(self.http.calls),1)
        self.http=cloud();self.setup=CloudSetup(self.http)
        await self.login();self.http.documents['groups/group_a']['group']['devices']=['missing']
        with self.assertRaises(ImportSetupError):await self.setup.preview(self.session,'home')
        self.assertIsNone(self.setup.plan)
    async def test_cancel_during_request_invalidates_late_result(self):
        entered,release=asyncio.Event(),asyncio.Event()
        original=self.setup._locations
        async def slow(*args):
            entered.set();await release.wait();return await original(*args)
        with patch.object(self.setup,'_locations',side_effect=slow):
            task=asyncio.create_task(self.login());await entered.wait();self.setup.cancel();release.set()
            with self.assertRaises(ImportSetupError):await task
        self.assertIsNone(self.setup.token);self.assertIsNone(self.setup.plan)
    async def test_lost_save_consumes_preview_and_never_repeats(self):
        preview=await self.preview();attempts=[]
        def save(plan):attempts.append(1);raise OSError('disk failure')
        with self.assertRaises(OSError):await self.setup.apply(self.session,preview['preview'],save)
        with self.assertRaises(ImportSetupError):await self.setup.apply(self.session,preview['preview'],save)
        self.assertEqual(attempts,[1])
    async def test_busy_requests_and_confirmation_are_bounded(self):
        await self.setup.lock.acquire()
        try:
            with self.assertRaises(ImportSetupError):await self.login()
        finally:self.setup.lock.release()
        self.assertEqual(self.http.calls,[])
    async def test_group_member_aliases_match_esp_without_accepting_ambiguity(self):
        for field in ('avid','avion_id','device_id','id'):
            group=copy.deepcopy(GROUP);group['group']['devices']=[{field:40002}]
            self.assertEqual(parse_inventory(DEVICES,[group])['groups'][0]['members'],[40002])
        group=copy.deepcopy(GROUP);group['group']['devices']=[{'avid':40002,'id':40003}]
        with self.assertRaises(ValueError):parse_inventory(DEVICES,[group])


class ImportedInstallationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'data'
        self.lock=q.installation_lock(self.root);self.lock.__enter__()
        with redirect_stdout(io.StringIO()):self.token=q.password(self.root)
        self.http=cloud();self.setup=CloudSetup(self.http);self.ready=asyncio.Event()
        self.headers={'Host':HOST+':18089','Authorization':basic_header('halo',self.token)}
        self.client=TestClient(TestServer(q.setup_app(self.root,HOST,18089,'hci0',self.token,self.ready,cloud_setup=self.setup,browser_login=True)))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.lock.__exit__(None,None,None);self.temp.cleanup()
    async def request(self,action,payload):
        response=await self.client.post('/api/setup/cloud/'+action,json=payload,headers=self.headers)
        return response,await response.json()
    async def prepare(self):
        response,login=await self.request('login',{'email':'owner@example.invalid','password':'synthetic-password'})
        self.assertEqual(response.status,200)
        response,preview=await self.request('preview',{'session':login['session'],'location':'home'})
        self.assertEqual(response.status,200)
        return {'session':login['session'],'preview':preview['preview'],'enable_local_control':True}
    def store(self):return Store(self.root/'bridge/state',derive_mcp_key(PHRASE))
    async def test_import_restart_control_and_same_mesh_sync(self):
        payload=await self.prepare()
        self.assertFalse((self.root/'bridge').exists())
        response,result=await self.request('apply',payload)
        self.assertEqual(response.status,201);self.assertTrue(self.ready.is_set())
        self.assertEqual((self.root/'bridge/mesh_passphrase').read_text().strip(),PHRASE)
        cfg=q.existing_config(self.root,HOST,18089,'hci0')
        self.assertTrue(cfg['automatic_relay']);self.assertFalse(cfg['first_device_bootstrap'])
        self.assertTrue(cfg['capabilities']['removal'])
        self.assertFalse(cfg['capabilities']['claim_reset_recovery'])
        self.assertTrue(automatic_relay_mode(cfg))
        store=self.store()
        try:
            store.require_tx_ready();receipt=store.get('tx_admission')
            self.assertFalse(receipt['document']['history_complete'])
            self.assertFalse(status(store)['historical_floor_known'])
            transport=FakeTransport();transport.send=AsyncMock(side_effect=transport.frames.append);runtime=Runtime(store,derive_mcp_key(PHRASE),transport)
            runtime.mesh_ready=True
            await runtime.control(291,{'state':'ON'},confirm=False,local=True)
            self.assertEqual(len(transport.frames),2)
            packet=decrypt_mcp(transport.frames[0],derive_mcp_key(PHRASE))
            self.assertEqual(packet.mcp_address,0)
            self.assertEqual(int.from_bytes(packet.payload[2:4],"big"),291)
            self.assertEqual(packet.payload[:2],bytes([0,10]))
            feedback=decrypt_mcp(transport.frames[1],derive_mcp_key(PHRASE))
            self.assertEqual(feedback.mcp_address,40002)
            self.assertEqual(feedback.payload[:2],bytes([1,10]))
            self.assertEqual(store.get('sequence'),2)
            runtime.capabilities=cfg['capabilities']
            sync=CloudImport(runtime,self.http)
            # Existing import may compare every location, so provide the second.
            self.http.documents['locations/other']={'location':{'passphrase':'different-synthetic-key'}}
            preview=await sync.preview('owner@example.invalid','synthetic-password','home')
            self.assertEqual(preview['status'],'ready')
            synced=await sync.apply({'preview_id':preview['preview_id'],'revision':preview['revision']})
            self.assertFalse(synced['physical_memberships_changed'])
            self.assertEqual(store.get('sequence'),2)
        finally:store.close()
        q.existing_config(self.root,HOST,18089,'hci0')
        store=self.store()
        try:
            self.assertEqual(store.allocate_sequence(),3)
            self.assertEqual(store.get('tx_admission'),receipt)
            with self.assertRaises(ValueError):admit_cloud_import(store,location_id='home',inventory_digest='a'*64)
            with self.assertRaises(RuntimeError):Store(self.root/'bridge/state',derive_mcp_key(PHRASE))
            metadata=' '.join(v for (v,) in store.db.execute('SELECT value FROM metadata'))
            for secret in (PHRASE,'synthetic-password','synthetic-login-token'):self.assertNotIn(secret,metadata)
            retire(store)
            with self.assertRaises(RuntimeError):store.allocate_sequence()
        finally:store.close()
        with self.assertRaises(q.SetupError):q.existing_config(self.root,HOST,18089,'hci0')
    async def test_confirmation_auth_and_existing_installation_guard(self):
        payload=await self.prepare();bad=dict(payload,enable_local_control=False)
        response,_=await self.request('apply',bad);self.assertEqual(response.status,409)
        response=await self.client.post('/api/setup/cloud/apply',json=payload,headers={'Host':HOST+':18089'})
        self.assertEqual(response.status,401);self.assertFalse((self.root/'bridge').exists())
        q.initialize(self.root,HOST,18089,'hci0');before=(self.root/'bridge/mesh_passphrase').read_bytes()
        response,_=await self.request('apply',payload);self.assertEqual(response.status,409)
        self.assertEqual((self.root/'bridge/mesh_passphrase').read_bytes(),before)
    async def test_interrupted_import_keeps_private_stage_and_does_not_restart(self):
        payload=await self.prepare()
        with patch.object(q.os,'rename',side_effect=OSError('synthetic failure')):
            response,_=await self.request('apply',payload)
        self.assertEqual(response.status,409);self.assertFalse(self.ready.is_set())
        key=self.root/'.initializing/mesh_passphrase'
        self.assertEqual(key.read_text().strip(),PHRASE);self.assertEqual(key.stat().st_mode&0o777,0o600)
        response,_=await self.request('apply',payload);self.assertEqual(response.status,409)
        self.assertFalse((self.root/'bridge').exists())
    async def test_default_no_login_cloud_import_persists_lan_access_mode(self):
        await self.client.close()
        self.client=TestClient(TestServer(q.setup_app(self.root,HOST,18089,'hci0',self.token,self.ready,cloud_setup=self.setup)))
        await self.client.start_server()
        self.headers={'Host':HOST+':18089'}
        payload=await self.prepare()
        response,result=await self.request('apply',payload)
        self.assertEqual(response.status,201)
        config=json.loads((self.root/'bridge/service.json').read_text())
        self.assertFalse(config['browser_login'])
        self.assertTrue(config['trusted_lan_no_auth'])

    async def test_setup_assets_are_authenticated_and_no_store(self):
        for path in ('/','/setup.js','/setup.css'):
            response=await self.client.get(path,headers={'Host':HOST+':18089'})
            self.assertEqual(response.status,401)
            response=await self.client.get(path,headers=self.headers)
            self.assertEqual(response.status,200);self.assertEqual(response.headers['Cache-Control'],'no-store')
