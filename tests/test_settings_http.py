"""Settings HTTP boundaries: synthetic credentials, fake cloud and no radio."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import test_http as http
from avionmesh_pi.cloud import CloudImport
from avionmesh_pi.mqtt_settings import MqttSettings
from test_cloud import FakeCloud, DEVICES, GROUP


@unittest.skipIf(http.create_app is None, 'HTTP dependencies run in isolated container')
class SettingsHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self)
        self.manager = MqttSettings(self.runtime, self.temp.name)
        self.runtime.mqtt_settings = self.manager
        self.cloud = CloudImport(self.runtime, FakeCloud({
            'sessions': {'credentials': {'auth_token': 'synthetic-cloud-token'}},
            'user/locations': {'locations': [{'pid': 'house', 'name': 'Old house'}]},
            'locations/house': {'location': {'name': 'Old house', 'passphrase': 'different-synthetic-mesh'}},
            'locations/house/abstract_devices': DEVICES,
            'locations/house/groups': {'groups': [{'pid': 'group_a'}]},
            'groups/group_a': GROUP,
        }))
        await self.client.close()
        self.client = http.TestClient(http.TestServer(http.create_app(
            self.runtime, self.token, cloud_import=self.cloud)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.manager.close()
        await http.HttpTests.asyncTearDown(self)

    async def test_software_update_check_install_and_read_only_guards(self):
        base = '/api/settings/update'
        response = await self.client.post(base + '/check', json={})
        self.assertEqual(response.status, 401)
        with patch('avionmesh_pi.release_update.latest_release', return_value='9.0.0.dev1') as lookup:
            response = await self.client.get(base, headers=self.headers)
            self.assertIsNone((await response.json())['latest'])
            lookup.assert_not_called()
            response = await self.client.post(base+'/check', headers=self.headers, json={})
            self.assertTrue((await response.json())['available'])
            response = await self.client.post(base+'/install', headers=self.headers, json={'version':'9.0.0.dev1'})
            self.assertEqual(response.status, 400)
            with patch.object(self.store, 'journals', return_value=[{'kind':'claim'}]):
                response = await self.client.post(base+'/install', headers=self.headers,
                    json={'version':'9.0.0.dev1', 'confirm_restart':True})
                self.assertEqual(response.status, 409)
                self.assertIn('operation', (await response.json())['message'])
            preview = http.TestClient(http.TestServer(http.create_app(self.runtime, self.token, read_only=True)))
            async with preview:
                self.assertEqual((await preview.post(base+'/install', headers=self.headers, json={})).status, 403)
            lookup.assert_called_once()

    async def get_settings(self):
        response = await self.client.get('/api/settings/mqtt', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return await response.json()

    def payload(self, revision, **changes):
        return dict({'revision': revision, 'enabled': False, 'host': 'broker.example.invalid',
                     'port': 8883, 'username': 'synthetic-user', 'tls': True,
                     'password_action': 'replace', 'password': 'synthetic-secret-settings'}, **changes)

    async def test_save_redacts_password_from_every_public_endpoint_and_preserves_mesh(self):
        before = self.store.inventory(), self.store.get('sequence'), self.runtime.generation
        settings = await self.get_settings()
        response = await self.client.post('/api/settings/mqtt', headers=self.headers,
                                          json=self.payload(settings['revision']))
        self.assertEqual(response.status, 200, await response.text())
        saved = await response.json()
        self.assertTrue(saved['saved'])
        self.assertTrue((await self.get_settings())['password_set'])
        for endpoint in ('/api/settings/mqtt', '/api/snapshot', '/api/status', '/api/inventory', '/api/transactions'):
            body = await (await self.client.get(endpoint, headers=self.headers)).text()
            self.assertNotIn('synthetic-secret-settings', body)
        self.assertEqual((self.store.inventory(), self.store.get('sequence'), self.runtime.generation), before)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(Path(self.temp.name, 'mqtt-settings.json').stat().st_mode & 0o777, 0o600)
        response = await self.client.post('/api/settings/mqtt', headers=self.headers,
                                          json=self.payload(settings['revision']))
        self.assertEqual(response.status, 409)

    async def test_auth_origin_and_json_guards_precede_settings_or_cloud_work(self):
        for endpoint in ('/api/settings/mqtt', '/api/settings/mqtt/test',
                         '/api/cloud-import/preview', '/api/cloud-import/apply'):
            with self.subTest(endpoint=endpoint):
                response = await self.client.post(endpoint, json={})
                self.assertEqual(response.status, 401)
                response = await self.client.post(endpoint, headers=dict(self.headers, Origin='http://foreign.invalid'), json={})
                self.assertEqual(response.status, 403)
                response = await self.client.post(endpoint, headers=self.headers, data='{}')
                self.assertEqual(response.status, 415)
        self.assertFalse(Path(self.temp.name, 'mqtt-settings.json').exists())
        self.assertEqual(self.transport.frames, [])

    async def test_cloud_key_mismatch_is_visible_and_cannot_replace_current_inventory(self):
        before = self.store.inventory(), self.store.get('sequence')
        response = await self.client.post('/api/cloud-import/preview', headers=self.headers,
                                          json={'email': 'example@example.invalid', 'password': 'synthetic-cloud-password'})
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(data['status'], 'blocked')
        self.assertFalse(data['can_apply'])
        self.assertIsNone(data['preview_id'])
        for secret in ('synthetic-cloud-password', 'synthetic-cloud-token', 'different-synthetic-mesh'):
            self.assertNotIn(secret, json.dumps(data))
        self.assertEqual((self.store.inventory(), self.store.get('sequence')), before)
        self.assertEqual(self.transport.frames, [])

    async def test_network_preview_does_not_pause_local_observation_scheduler(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending(*args):
            entered.set()
            await release.wait()
            return {'status': 'blocked', 'can_apply': False}
        with patch.object(self.cloud, 'preview', side_effect=pending):
            task = asyncio.create_task(self.client.post('/api/cloud-import/preview', headers=self.headers,
                json={'email': 'example@example.invalid', 'password': 'synthetic-cloud-password'}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(self.runtime._foreground_requests, 0)
                self.assertFalse(self.runtime.operation.locked())
            finally:
                release.set()
                self.assertEqual((await task).status, 200)

    async def test_read_only_preview_cannot_submit_any_settings_or_cloud_request(self):
        await self.client.close()
        self.client = http.TestClient(http.TestServer(http.create_app(
            self.runtime, self.token, read_only=True, cloud_import=self.cloud)))
        await self.client.start_server()
        for endpoint in ('/api/settings/mqtt', '/api/settings/mqtt/test',
                         '/api/cloud-import/preview', '/api/cloud-import/apply'):
            response = await self.client.post(endpoint, headers=self.headers, json={})
            self.assertEqual(response.status, 403)
        self.assertFalse(Path(self.temp.name, 'mqtt-settings.json').exists())

