"""In-place conversion on an admitted synthetic HALO mesh, with no pairing receipt."""
import asyncio
import copy
import tempfile
import unittest

import test_http as http
from test_dimmer_control import Transport
from test_service import KEY
from avionmesh_pi.cloud_provenance import retain_cloud_devices
from avionmesh_pi.controller_workflow import controller_workflows
from avionmesh_pi.dimmer import DimmerEditor
from avionmesh_pi.errors import OperationNotStarted
from avionmesh_pi.migration import admit_cloud_import, canonical_digest
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.storage import Store


class ImportedDimmerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        inventory = {'devices': [
            {'device_id': 40001, 'product_id': 91, 'name': 'Imported accessory', 'exposed': False},
            {'device_id': 40002, 'product_id': 93, 'name': 'Imported lamp', 'exposed': False}],
            'groups': [{'group_id': 260, 'name': 'Cloud group', 'members': [40002],
                        'protected': False, 'exposed': False}]}
        admit_cloud_import(self.store, location_id='fixture', inventory_digest=canonical_digest(inventory))
        self.store.import_inventory(inventory)
        self.store.set('cloud_location', {'id': 'fixture'})
        retain_cloud_devices(self.store, inventory['devices'], 'fixture')
        self.start_runtime()

    def start_runtime(self):
        self.now = 100
        self.radio = Transport()
        self.runtime = Runtime(self.store, KEY, self.radio, clock=lambda: self.now)
        self.radio.runtime = self.runtime
        self.runtime.capabilities.update(dimmer_editor=True, inventory_complete=True)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = self.runtime.mqtt_enabled = True
        self.runtime.session()
        self.editor = DimmerEditor(self.runtime)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def view(self):
        return controller_workflows(self.runtime)[0]

    def preview(self):
        return self.editor.preview_configuration(40001, 'ha')

    async def submit(self, review, timeout=1):
        return await self.editor.configure(40001, 'ha', expected_revision=review['revision'],
            expected_baseline=review['baseline'], timeout=timeout)

    @unittest.skipIf(http.create_app is None, 'HTTP dependencies run in the isolated image')
    async def test_ui_endpoints_review_convert_accept_and_restart(self):
        headers = {'Authorization': 'Bearer synthetic-import-control-test-token-12345'}
        app = http.create_app(self.runtime, headers['Authorization'][7:])
        async with http.TestClient(http.TestServer(app)) as client:
            controller = (await (await client.get('/api/snapshot', headers=headers)).json())['controllers'][0]
            self.assertTrue(controller['imported_ha_control'])
            self.assertTrue(controller['actions']['configure_control']['enabled'])
            self.assertFalse(controller['actions']['setup_ha']['enabled'])
            self.radio.command(270)  # Existing on-air group omitted by the original app's import.
            body = {'target': 40001, 'mode': 'ha'}
            preview = await client.post('/api/dimmer/preview', headers=headers, json=body)
            self.assertEqual(preview.status, 200)
            review = await preview.json()
            self.assertEqual(review['source_group'], 270)
            self.assertEqual(self.radio.frames, [])
            self.assertEqual(self.store.journals(), [])
            response = await client.post('/api/dimmer/configure', headers=headers,
                json=body | {key: review[key] for key in ('revision','baseline')})
            self.assertEqual(response.status, 202)
            async with asyncio.timeout(2):
                while True:
                    job = (await (await client.get('/api/snapshot', headers=headers)).json())['status']['jobs']['40001']
                    if job['state'] != 'running':
                        break
                    await asyncio.sleep(0)
            self.assertEqual(job['state'], 'completed', job)
            self.assertTrue(job['result']['acceptance_required'])
            self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
            response = await client.post('/api/dimmer/accept', headers=headers,
                json={'transaction': job['result']['transaction']})
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.radio.frames), 1)
        receipt, = self.store.journals()
        self.assertEqual(receipt['detail']['cloud_source'], self.store.get('cloud_device_sources')['40001'])
        self.assertNotIn('claim_receipt', receipt['detail'])
        self.assertNotIn('claim', receipt['detail'])
        self.assertEqual(self.store.inventory()['groups'][0]['members'], [40002])
        self.assertEqual(self.store.inventory()['groups'][1]['members'], [])
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.start_runtime()
        self.assertEqual(self.view()['control_configuration']['mode'], 'ha')
        events = []
        self.runtime.sink = events.append
        for level in (255, 96, 0):
            self.radio.command(review['group'], level)
        published = [message['payload'] for message in events if message['topic'].endswith('/event/40001/event')]
        self.assertEqual([(event['event_type'], event['brightness'], event['target']) for event in published],
                         [(kind, level, review['group']) for kind, level in [('on', 255), ('brightness', 96), ('off', 0)]])
        self.assertEqual(self.radio.frames, [])
        # A later change made in HALO Home can reuse the same empty destination.
        self.radio.command(270)
        again = self.preview()
        self.assertTrue(again['reuses_ha_target'])
        self.assertEqual(again['group'], review['group'])

    async def test_requires_matching_import_fresh_input_and_mqtt(self):
        with self.assertRaises(ValueError):
            self.preview()
        self.radio.command(260)
        review = self.preview()
        self.now += 15001
        with self.assertRaises(OperationNotStarted):
            await self.submit(review)
        self.radio.command(260)
        with self.assertRaises(ValueError):
            self.editor.preview_configuration(40001, 'native', 260)
        self.runtime.mqtt_ready = False
        with self.assertRaises(ValueError):
            self.preview()
        self.runtime.mqtt_ready = True
        review = self.preview()
        sources = self.store.get('cloud_device_sources')
        for key, wrong in [('admission_id', 'stale'), ('state_id', 'stale'), ('product_id', 81),
                           ('mesh_fingerprint', 'stale'), ('authority_type', 'legacy_imported_inventory_owner_selected')]:
            altered = copy.deepcopy(sources)
            altered['40001'][key] = wrong
            self.store.set('cloud_device_sources', altered)
            with self.subTest(key=key):
                self.assertFalse(self.view()['actions']['configure_control']['enabled'])
                with self.assertRaises(OperationNotStarted):
                    await self.submit(review)
        self.store.set('cloud_device_sources', {})
        self.assertFalse(self.view()['imported_ha_control'])
        self.store.set('cloud_device_sources', sources)
        self.store.set('retired_addresses', [40001])
        self.assertFalse(self.view()['actions']['configure_control']['enabled'])
        self.store.set('retired_addresses', [])
        self.store.begin('claim', 40001, {})
        self.assertFalse(self.view()['imported_ha_control'])
        self.assertEqual(self.radio.frames, [])

    async def test_direct_light_baseline_and_recovery_never_repeats_write(self):
        self.radio.command(40002)
        review = self.preview()
        self.assertEqual(self.view()['observed_target']['target'], 40002)
        self.radio.native = False
        with self.assertRaises(asyncio.TimeoutError):
            await self.submit(review, timeout=.02)
        receipt, = self.store.journals()
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertEqual(len(self.radio.frames), 1)
        self.runtime.mesh_ready = self.radio.ready = True
        self.runtime.session()
        self.radio.sequence += 10
        recovery = asyncio.create_task(self.editor.reconcile(receipt['id'], timeout=1))
        await asyncio.sleep(0)
        self.radio.command(review['group'])
        self.assertFalse((await recovery)['write_retried'])
        self.editor.accept_observed(receipt['id'])
        self.assertEqual(len(self.radio.frames), 1)

    async def test_older_import_can_refresh_its_cloud_record_without_pairing(self):
        sources = self.store.get('cloud_device_sources')
        sources['40001']['authority_type'] = 'legacy_imported_inventory_owner_selected'
        self.store.set('cloud_device_sources', sources)
        self.radio.command(260)
        self.assertFalse(self.view()['actions']['configure_control']['enabled'])
        retain_cloud_devices(self.store, self.store.inventory()['devices'], 'fixture')
        self.assertTrue(self.view()['actions']['configure_control']['enabled'])
        self.assertEqual(self.preview()['source_group'], 260)
        self.assertEqual(self.store.journals(), [])

    async def test_protected_foreign_target_and_another_devices_press_cannot_start_conversion(self):
        inventory = self.store.inventory()
        inventory['groups'].append({'group_id': 301, 'name': 'Private fixture group',
                                    'members': [], 'protected': True, 'exposed': False})
        self.store.set('inventory', inventory)
        self.radio.command(301)
        with self.assertRaises(ValueError):
            self.preview()
        self.radio.command(260)
        review = self.preview()
        self.radio.command(270)
        with self.assertRaises(OperationNotStarted):
            await self.submit(review)
        self.runtime.session()
        self.radio.emit(bytes.fromhex('000a01040100ff00000000'), source=40002)
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(self.radio.frames, [])

    async def test_review_and_accept_are_bound_to_the_same_import(self):
        self.radio.command(260)
        result = await self.submit(self.preview())
        sources = self.store.get('cloud_device_sources')
        sources['40001']['location_id'] = 'changed'
        self.store.set('cloud_device_sources', sources)
        with self.assertRaises(ValueError):
            self.editor.accept_observed(result['transaction'])
        self.assertEqual(self.view()['control_configuration']['state'], 'needs_review')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(len(self.radio.frames), 1)
