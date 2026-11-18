import json
import tempfile
import unittest

try:
    from aiohttp.test_utils import TestClient, TestServer
    from avionmesh_pi.server import create_app
except ImportError:
    create_app = None

from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY, INVENTORY, FakeTransport


@unittest.skipIf(create_app is None, "HTTP dependencies are tested in the container")
class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.transport.receiver = self.runtime.observe
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        self.token = "synthetic-test-token-01234567890123456789"
        self.client = TestClient(TestServer(create_app(self.runtime, self.token)))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    async def test_new_claim_receipt_is_recovered_without_second_association(self):
        from unittest.mock import patch, AsyncMock
        from types import SimpleNamespace
        self.runtime.capabilities['ordinary_claim'] = True
        self.runtime.bootstrap = SimpleNamespace(automatic=True)
        rows = []
        async def fail(*args, **kwargs):
            rows.append({'id':'new','kind':'claim','status':'uncertain','detail':{'uuid':'00'*16}})
            raise asyncio.TimeoutError()
        import asyncio
        with patch.object(self.store,'journals',side_effect=lambda *a,**k: list(rows)), patch('avionmesh_pi.commissioning.Commissioning.claim',side_effect=fail) as claim, patch('avionmesh_pi.commissioning.Commissioning.recover_safe_pending',new=AsyncMock(return_value={'outcome':'verified','result':{'associated':True,'device_id':40003}})) as recover:
            response = await self.client.post('/api/claim',headers=self.headers,json={'uuid':'00'*16,'name':'Synthetic','product_id':93})
        self.assertEqual(response.status,200)
        self.assertTrue((await response.json())['recovery_completed'])
        self.assertEqual(claim.call_count,1)
        recover.assert_awaited_once_with('new')

    async def test_retired_but_refresh_failed_does_not_invite_retry(self):
        import asyncio
        from unittest.mock import patch, AsyncMock
        from types import SimpleNamespace
        self.runtime.capabilities['ordinary_claim'] = True
        self.runtime.bootstrap = SimpleNamespace(automatic=True)
        rows = []
        async def fail(*args, **kwargs):
            rows.append({'id':'new','kind':'claim','status':'uncertain','detail':{'uuid':'00'*16}})
            raise asyncio.TimeoutError()
        with patch.object(self.store,'journals',side_effect=lambda *a,**k:list(rows)), patch('avionmesh_pi.commissioning.Commissioning.claim',side_effect=fail), patch('avionmesh_pi.commissioning.Commissioning.recover_safe_pending',new=AsyncMock(return_value={'outcome':'pre_key_retired','requires_review':True})):
            response = await self.client.post('/api/claim',headers=self.headers,json={'uuid':'00'*16,'name':'Synthetic','product_id':93})
        body = await response.json()
        self.assertEqual(response.status,409)
        self.assertEqual(body['error'],'claim_verification_pending')
        self.assertNotIn('click Claim again',body['message'])

    async def test_claim_validation_has_actionable_message_and_no_start(self):
        from unittest.mock import patch, AsyncMock
        from avionmesh_pi.commissioning import ClaimValidationError
        self.runtime.capabilities['ordinary_claim'] = True
        with patch('avionmesh_pi.commissioning.Commissioning.claim', new=AsyncMock(side_effect=ClaimValidationError('Selected light was not seen. No pairing was started.'))):
            response = await self.client.post('/api/claim',headers=self.headers,json={'uuid':'00'*16,'name':'Synthetic','product_id':93})
        body = await response.json()
        self.assertEqual(response.status,409)
        self.assertEqual(body['error'],'claim_not_started')
        self.assertFalse(body['association_started'])
        self.assertIn('No pairing was started',body['message'])

    async def test_protocol_error_is_not_invalid_request(self):
        from unittest.mock import patch, AsyncMock
        from avionmesh_pi.association import AssociationError
        self.runtime.capabilities['ordinary_claim'] = True
        with patch('avionmesh_pi.commissioning.Commissioning.claim', new=AsyncMock(side_effect=AssociationError('association_response_rejected'))):
            response = await self.client.post('/api/claim', headers=self.headers,
                json={'uuid':'00'*16,'name':'Synthetic','product_id':93})
        self.assertEqual(response.status, 409)
        body = await response.json()
        self.assertEqual(body['error'], 'association_response_rejected')
        self.assertEqual(body['outcome'], 'uncertain')
        self.assertFalse(body['retry_allowed'])

    async def test_authentication_and_redacted_status(self):
        self.assertEqual((await self.client.get('/api/status')).status, 401)
        response = await self.client.get('/api/status', headers=self.headers)
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertNotIn(self.token, body)
        self.assertNotIn(KEY.hex(), body)

    async def test_http_control_reaches_fake_ble_and_readback(self):
        response = await self.client.post('/api/control', headers=self.headers,
                                          json={"target": 40002, "values": {"brightness": 128}})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["confirmed"])
        self.assertEqual(len(self.transport.frames), 2)

    async def test_async_control_returns_without_waiting_for_reports(self):
        from unittest.mock import patch, AsyncMock
        with patch.object(self.runtime, '_observation_task', object()), patch.object(
                self.runtime, 'exchange', new=AsyncMock(side_effect=AssertionError('Must not await reports'))):
            response = await self.client.post('/api/control', headers=self.headers,
                json={'target':40002, 'values':{'brightness':128}, 'confirm':False})
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertTrue(result['sent'])
        self.assertTrue(result['feedback_pending'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['readbacks'], [])
        self.assertEqual(len(self.transport.frames), 1)
        self.assertIn(40002, self.runtime._group_refresh)

    async def test_async_control_flag_rejects_invalid_values_before_transmit(self):
        for flag in (None, 0, 1, 'false', [], {}):
            response = await self.client.post('/api/control', headers=self.headers,
                json={'target':40002, 'values':{'brightness':128}, 'confirm':flag})
            self.assertEqual(response.status, 400)
        self.assertEqual(self.transport.frames, [])

    async def test_foreign_origin_and_bad_target_fail_before_transmit(self):
        response = await self.client.post('/api/control', headers=dict(self.headers, Origin='https://foreign.invalid'),
                                          json={"target": 40002, "values": {"state": "ON"}})
        self.assertEqual(response.status, 403)
        response = await self.client.post('/api/control', headers=self.headers,
                                          json={"target": 292, "values": {"state": "ON"}})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.transport.frames, [])

    async def test_commissioning_and_editor_default_disabled(self):
        for route in ('claim', 'discover', 'keypad-configure', 'unclaim'):
            response = await self.client.post('/api/' + route, headers=self.headers, json={})
            if route == 'unclaim':
                self.assertEqual(response.status, 409)
                body = await response.json()
                self.assertEqual(body['error'], 'removal_disabled')
                self.assertFalse(body['operation_started'])
                self.assertFalse(body['retry_allowed'])
            else:
                self.assertEqual(response.status, 501)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def test_metadata_import_preserves_protected_group(self):
        changed = json.loads(json.dumps(INVENTORY))
        changed['groups'] = changed['groups'][:1]
        response = await self.client.post('/api/import', headers=self.headers, json=changed)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.store.inventory(), INVENTORY)

    async def test_unexpected_background_failure_is_reported_without_details(self):
        from unittest.mock import patch
        from avionmesh_pi.onboarding import KeypadOnboarding
        import asyncio
        self.runtime.capabilities.update(wired_onboarding=True,inventory_complete=True)
        with patch.object(KeypadOnboarding,'setup',side_effect=OSError('synthetic secret must not leak')):
            response = await self.client.post('/api/controller/setup',headers=self.headers,json={'target':40001})
            self.assertEqual(response.status,202)
            await asyncio.sleep(0)
            response = await self.client.get('/api/status',headers=self.headers)
            body = await response.text()
            self.assertIn('failed_needs_review',body)
            self.assertNotIn('synthetic secret',body)

    async def test_setup_request_is_rejected_before_claim_when_capability_disabled(self):
        self.runtime.capabilities['ordinary_claim'] = True
        response = await self.client.post('/api/claim',headers=self.headers,
            json={'uuid':'01'*16,'name':'Synthetic','product_id':91,'setup_ha':True})
        self.assertEqual(response.status,400)
        self.assertEqual(self.transport.frames,[])
        self.assertEqual(self.store.journals(),[])

    async def test_capture_requires_matching_owner(self):
        response = await self.client.post('/api/capture',headers=self.headers,json={'target':40001,'seconds':30})
        self.assertEqual(response.status,200)
        identifier = (await response.json())['id']
        self.assertEqual((await self.client.get('/api/capture/'+identifier,headers=self.headers)).status,200)
        self.assertEqual((await self.client.get('/api/capture/wrong',headers=self.headers)).status,400)

    async def test_group_capture_uses_frozen_inventory_without_sending(self):
        response = await self.client.post('/api/capture', headers=self.headers,
                                          json={'group':291,'seconds':30})
        self.assertEqual(response.status, 200)
        capture = await response.json()
        self.assertEqual(capture['scope'], 'group')
        self.assertEqual(capture['group'], 291)
        self.assertEqual(capture['members'], [40002])
        exported = await self.client.get('/api/capture/' + capture['id'], headers=self.headers)
        self.assertEqual(exported.status, 200)
        self.assertEqual((await exported.json())['records'], [])
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def test_group_capture_rejects_unknown_protected_and_injected_members(self):
        invalid = [{'group':292,'seconds':30}, {'group':293,'seconds':30},
                   {'group':True,'seconds':30}, {'group':291,'seconds':61},
                   {'group':291,'seconds':30,'members':[40001]},
                   {'group':291,'target':40001,'seconds':30}, [], None]
        for body in invalid:
            with self.subTest(body=body):
                response = await self.client.post('/api/capture',
                    headers=dict(self.headers, **{'Content-Type':'application/json'}), data=json.dumps(body))
                self.assertEqual(response.status, 400)
                self.assertIsNone(self.runtime.diagnostics.capture)
        self.assertEqual(self.transport.frames, [])

    async def test_group_capture_rejects_empty_or_controller_members(self):
        from unittest.mock import patch
        for members in ([], [40001], [40003]):
            inventory = json.loads(json.dumps(INVENTORY))
            inventory['groups'][0]['members'] = members
            with self.subTest(members=members), patch.object(self.store, 'inventory', return_value=inventory):
                response = await self.client.post('/api/capture', headers=self.headers,
                                                  json={'group':291,'seconds':30})
                self.assertEqual(response.status, 400)
                self.assertIsNone(self.runtime.diagnostics.capture)
        self.assertEqual(self.transport.frames, [])

    async def test_snapshot_revision_metadata_and_capture_owner_actions(self):
        response=await self.client.get('/api/snapshot',headers=self.headers)
        data=await response.json()
        response=await self.client.post('/api/metadata',headers=self.headers,json={
            'operation':'update','revision':data['status']['inventory_revision'],'kind':'device',
            'target':40002,'name':'Renamed','exposed':False})
        self.assertEqual(response.status,200)
        self.assertFalse((await response.json())['physical_memberships_changed'])
        response=await self.client.post('/api/capture',headers=self.headers,json={'target':40001,'seconds':30})
        owner=(await response.json())['id']
        response=await self.client.post('/api/capture/action',headers=self.headers,json={'id':'wrong','action':'stop'})
        self.assertEqual(response.status,400)
        response=await self.client.post('/api/capture/action',headers=self.headers,json={'id':owner,'action':'stop'})
        self.assertTrue((await response.json())['complete'])
        self.assertEqual(self.transport.frames,[])

    async def test_stream_limit_snapshot_and_prompt_server_shutdown(self):
        import asyncio
        responses=[]
        try:
            for _ in range(4):
                response=await self.client.get('/api/events',headers=self.headers)
                self.assertEqual(response.status,200)
                block=await asyncio.wait_for(response.content.readuntil(b'\n\n'),2)
                self.assertIn(b'event: snapshot',block)
                self.assertIn(b'"resync": true',block)
                self.assertNotIn(self.token.encode(),block)
                responses.append(response)
            rejected=await self.client.get('/api/events',headers=self.headers)
            self.assertEqual(rejected.status,503)
            await asyncio.wait_for(self.client.server.close(),3)
        finally:
            for response in responses:response.close()

    async def test_prepared_cancel_is_metadata_only_and_uncertain_is_blocked(self):
        identifier=self.store.begin('keypad_edit',40001,{'stages':[]})
        response=await self.client.post('/api/cancel',headers=self.headers,json={'transaction':identifier})
        self.assertEqual(response.status,200)
        self.assertEqual(self.store.journals()[0]['status'],'cancelled')
        identifier=self.store.begin('keypad_edit',40001,{'stages':['delete']})
        self.store.transition(identifier,'prepared','attempted')
        self.store.transition(identifier,'attempted','uncertain')
        response=await self.client.post('/api/cancel',headers=self.headers,json={'transaction':identifier})
        self.assertEqual(response.status,400)
        self.assertEqual(self.transport.frames,[])

    async def test_invalid_dimmer_jobs_and_deep_json_do_not_grow_state(self):
        self.runtime.capabilities['dimmer_editor']=True
        for index in range(100):
            response=await self.client.post('/api/dimmer/assign',headers=self.headers,json={'target':'bad-'+str(index),'group':291})
            self.assertEqual(response.status,400)
        response=await self.client.get('/api/status',headers=self.headers)
        self.assertEqual((await response.json())['jobs'],{})
        response=await self.client.post('/api/control',headers=dict(self.headers,**{'Content-Type':'application/json'}),data='['*1100+']'*1100)
        self.assertEqual(response.status,400)
        self.assertEqual(self.transport.frames,[])

    async def test_unexpected_keypad_read_error_is_observed_and_redacted(self):
        import asyncio
        from unittest.mock import patch
        from avionmesh_pi.keypad import KeypadService
        with patch.object(KeypadService,'read',side_effect=OSError('synthetic-secret-error')):
            response=await self.client.post('/api/keypad/read',headers=self.headers,json={'target':40001})
            self.assertEqual(response.status,202);await asyncio.sleep(0)
        response=await self.client.get('/api/status',headers=self.headers)
        text=await response.text()
        self.assertIn('failed_needs_review',text);self.assertNotIn('synthetic-secret-error',text)
