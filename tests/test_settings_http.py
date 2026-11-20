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

    async def test_relay_settings_scan_and_idle_reconnect(self):
        from avionmesh_pi.bootstrap import AutoBlueZTransport
        from test_bootstrap_automatic import advertisement, FIRST
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        route = '/api/settings/relay'
        scanner = SimpleNamespace(discover=AsyncMock(return_value={1: advertisement(FIRST, -53)}))
        transport = self.runtime.transport = AutoBlueZTransport(scanner=scanner)
        transport.ready, transport.address = True, FIRST
        self.assertEqual((await self.client.post(route+'/scan', json={})).status, 401)
        response = await self.client.get(route, headers=self.headers)
        value = await response.json()
        self.assertEqual(value['current'], FIRST)
        scanner.discover.assert_not_awaited()
        async with self.runtime.operation:
            response = await self.client.post(route+'/scan', headers=self.headers, json={})
            self.assertEqual(response.status, 409)
        scanner.discover.assert_not_awaited()
        response = await self.client.post(route+'/scan', headers=self.headers, json={})
        self.assertEqual(response.status, 200)
        value = await response.json()
        self.assertEqual(value['candidates'][0]['rssi'], -53)
        self.assertTrue(transport.ready)
        scanner.discover.side_effect = OSError('radio unavailable')
        self.assertEqual((await self.client.post(route+'/scan', headers=self.headers, json={})).status, 503)
        self.assertEqual(transport.observations[FIRST][0], -53)
        self.assertTrue(transport.ready)
        response = await self.client.post(route, headers=self.headers, json={'revision':value['revision'], 'preferred':None})
        self.assertEqual(response.status, 200)
        data = {'revision':(await response.json())['revision'], 'current':FIRST}
        with patch.object(self.store, 'journals', return_value=[{'kind':'claim'}]):
            self.assertEqual((await self.client.post(route+'/reconnect', headers=self.headers, json=data)).status, 409)
        self.assertTrue(transport.ready)
        self.assertEqual((await self.client.post(route+'/reconnect', headers=self.headers, json=data)).status, 202)
        self.assertFalse(transport.ready)
        self.assertFalse(self.runtime.mesh_ready)
        self.assertEqual(scanner.discover.await_count, 2)

    async def test_extended_scan_is_asynchronous_and_does_not_reserve_radio(self):
        from avionmesh_pi.bootstrap import AutoBlueZTransport
        entered = asyncio.Event()
        class Scanner:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                entered.set()
                return self
            async def __aexit__(self, *args):
                return False
        route = '/api/settings/relay/scan'
        transport = self.runtime.transport = AutoBlueZTransport(scanner=Scanner)
        transport.ready = True
        for value in (5, 60.0, True, '60'):
            self.assertEqual((await self.client.post(route, headers=self.headers, json={'seconds':value})).status, 400)
        response = await self.client.post(route, headers=self.headers, json={'seconds':60})
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())['extended_scan']['phase'], 'running')
        await asyncio.wait_for(entered.wait(), 1)
        try:
            self.assertFalse(self.runtime.operation.locked())
            self.assertEqual(self.runtime.status()['relay_extended_scan']['phase'], 'running')
            for body in ({}, {'seconds':60}):
                self.assertEqual((await self.client.post(route, headers=self.headers, json=body)).status, 409)
        finally:
            await transport.stop_extended_scan()

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

