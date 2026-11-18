import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

try:
    from aiohttp.test_utils import TestClient, TestServer
    from avionmesh_pi.server import create_app
except ImportError:
    create_app = None

from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY, FakeTransport


@unittest.skipIf(create_app is None, 'HTTP dependencies are tested in the container')
class AnyplaceHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 40001, 'product_id': 81, 'name': 'Synthetic Anyplace', 'exposed': False},
            {'device_id': 40002, 'product_id': 91, 'name': 'Synthetic Accessory', 'exposed': False}],
            'groups': [{'group_id': 291, 'name': 'Synthetic destination', 'members': [],
                        'protected': False, 'exposed': False}]})
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        token = 'synthetic-anyplace-token-01234567890123456789'
        self.headers = {'Authorization': 'Bearer ' + token}
        self.client = TestClient(TestServer(create_app(self.runtime, token)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    async def post(self, path, body):
        return await self.client.post('/api/' + path, headers=self.headers, json=body)

    async def test_legacy_accessory_flags_cannot_start_anyplace_setup_or_assignment(self):
        self.runtime.capabilities.update(accessory_onboarding=True, dimmer_editor=True,
                                         inventory_complete=True, ordinary_claim=True)
        for path, body in [('controller/setup', {'target': 40001}),
                           ('dimmer/assign', {'target': 40001, 'group': 291})]:
            response = await self.post(path, body)
            self.assertEqual(response.status, 501)
        response = await self.post('claim', {'uuid': '01' * 16, 'product_id': 81,
                                          'name': 'Synthetic', 'setup_ha': True})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.store.journals(), [])
        self.assertEqual(self.transport.frames, [])

    async def test_anyplace_flags_cannot_enable_accessory_writers(self):
        self.runtime.capabilities.update(anyplace_onboarding=True, anyplace_editor=True,
                                         inventory_complete=True)
        for path, body in [('controller/setup', {'target': 40002}),
                           ('dimmer/assign', {'target': 40002, 'group': 291})]:
            response = await self.post(path, body)
            self.assertEqual(response.status, 501)
        self.assertEqual(self.store.journals(), [])
        self.assertEqual(self.transport.frames, [])

    async def test_explicit_setup_dispatches_only_anyplace_and_returns_correlated_job(self):
        self.runtime.capabilities['anyplace_onboarding'] = True
        with patch('avionmesh_pi.anyplace.AnyplaceOnboarding.setup',
                   new=AsyncMock(return_value={'ready': True, 'transaction': 'synthetic-setup'})) as anyplace, \
             patch('avionmesh_pi.accessory.AccessoryOnboarding.setup', new=AsyncMock()) as accessory:
            response = await self.post('controller/setup', {'target': 40001})
            self.assertEqual(response.status, 202)
            accepted = await response.json()
            await asyncio.sleep(0)
            status = await (await self.client.get('/api/status', headers=self.headers)).json()
            self.assertEqual(status['jobs']['40001']['id'], accepted['job_id'])
            self.assertEqual(status['jobs']['40001']['result']['transaction'], 'synthetic-setup')
            self.assertIn('anyplace_progress', status)
            self.assertIn('claim_progress', status)
            anyplace.assert_awaited_once_with(40001)
            accessory.assert_not_awaited()
        self.assertEqual(self.transport.frames, [])

    async def test_uncertain_setup_reconciles_without_reenabling_writer_or_rerunning_setup(self):
        identifier = self.store.begin('anyplace_setup', 40001,
            {'product_id': 81, 'assignment_attempted': True})
        self.store.transition(identifier, 'prepared', 'attempted')
        self.store.transition(identifier, 'attempted', 'uncertain')
        with patch('avionmesh_pi.anyplace.AnyplaceOnboarding.reconcile',
                   new=AsyncMock(return_value={'ready': True, 'transaction': identifier,
                                              'write_retried': False})) as reconcile, \
             patch('avionmesh_pi.anyplace.AnyplaceOnboarding.setup', new=AsyncMock()) as setup:
            response = await self.post('reconcile', {'transaction': identifier})
            self.assertEqual(response.status, 202)
            await asyncio.sleep(0)
            reconcile.assert_awaited_once_with(identifier)
            setup.assert_not_awaited()
        self.assertEqual(self.transport.frames, [])

    async def test_actual_wake_timeout_job_proves_no_dispatch_and_allows_manual_review(self):
        self.runtime.capabilities.update(anyplace_onboarding=True, inventory_complete=True)
        claim = self.store.begin('claim', 40001, {'product_id': 81, 'uuid': '01' * 16})
        self.store.transition(claim, 'prepared', 'attempted')
        self.store.transition(claim, 'attempted', 'verified')
        with patch('avionmesh_pi.anyplace.AnyplaceOnboarding._deadline', return_value=20):
            response = await self.post('controller/setup', {'target': 40001})
            accepted = await response.json()
            self.assertEqual(response.status, 202)
            for _ in range(30):
                snapshot = await (await self.client.get('/api/snapshot', headers=self.headers)).json()
                job = snapshot['status']['jobs']['40001']
                if job['state'] != 'running':
                    break
                await asyncio.sleep(.01)
        self.assertEqual(job['id'], accepted['job_id'])
        self.assertEqual(job['state'], 'not_started')
        self.assertEqual(job['error'], 'anyplace_wake_timeout')
        self.assertFalse(job['operation_started'])
        self.assertFalse(job['requires_review'])
        self.assertFalse(job['retry_allowed'], 'A new reviewed request is never an automatic retry')
        view = next(v for v in snapshot['controllers'] if v['device_id'] == 40001)
        self.assertTrue(view['actions']['setup_ha']['enabled'])
        self.assertEqual(view['anyplace_ha']['state'], 'not_configured')
        self.assertEqual(self.store.journals()[-1]['status'], 'cancelled')
        self.assertEqual(self.transport.frames, [])
