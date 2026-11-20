"""Reviewed dimmer configuration HTTP contract; synthetic runtime only."""
import asyncio
import copy
import unittest
from unittest.mock import patch

import test_http as http
from avionmesh_pi.association import uuid_hash
from avionmesh_pi.errors import OperationNotStarted
from avionmesh_pi.lifecycle import reload_inventory


@unittest.skipIf(http.create_app is None, 'HTTP dependencies run in isolated container')
class DimmerConfigurationHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = http.HttpTests.asyncTearDown

    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self)
        inventory = self.store.inventory()
        inventory['devices'][0]['product_id'] = 91
        inventory['groups'].append({'group_id': 293, 'name': 'Second synthetic room',
            'members': [40002], 'exposed': False, 'protected': False})
        self.store.set('inventory', inventory)
        reload_inventory(self.runtime)
        self.runtime.capabilities.update(dimmer_editor=True, inventory_complete=True)
        self.claim = self.store.begin('claim', 40001, {'uuid': '01' * 16, 'product_id': 91,
            'uuid_hash': uuid_hash(bytes.fromhex('01' * 16)), 'association_complete': True})
        self.store.transition(self.claim, 'prepared', 'attempted')
        self.store.transition(self.claim, 'attempted', 'verified')
        self.observe_source()

    def observe_source(self):
        self.runtime.native_observations[40001] = ({'target': 291, 'sequence': 10,
            'target_generation': 1, 'payload': '000a012300ff00000000'}, 10)

    async def preview(self, body=None):
        return await self.client.post('/api/dimmer/preview', headers=self.headers,
            json=body if body is not None else {'target': 40001, 'mode': 'native', 'group': 293})

    async def reviewed(self, mode='native'):
        body = {'target': 40001, 'mode': mode}
        if mode == 'native':
            body['group'] = 293
        response = await self.preview(body)
        self.assertEqual(response.status, 200)
        review = await response.json()
        return body | {'baseline': review['baseline'], 'revision': review['revision']}

    async def configure(self, body):
        return await self.client.post('/api/dimmer/configure', headers=self.headers, json=body)

    async def assert_no_job(self):
        status = await (await self.client.get('/api/status', headers=self.headers)).json()
        self.assertNotIn('40001', status['jobs'])
        self.assertEqual(self.transport.frames, [])

    async def test_native_and_ha_preview_are_read_only_and_do_not_reserve_targets(self):
        inventory, journals = copy.deepcopy(self.store.inventory()), copy.deepcopy(self.store.journals())
        known = self.store.get('known_targets')
        for mode in ('native', 'ha'):
            with self.subTest(mode=mode):
                body = {'target': 40001, 'mode': mode}
                if mode == 'native':
                    body['group'] = 293
                response = await self.preview(body)
                self.assertEqual(response.status, 200)
                review = await response.json()
                self.assertEqual(review['mode'], mode)
                self.assertEqual(review['group'], 293 if mode == 'native' else 24575)
                self.assertEqual(review['baseline'], {'target': 291, 'observed_ms': 10, 'target_generation': 1})
                self.assertEqual(len(review['revision']), 64)
                self.assertFalse(review['assignment_read_back'])
                self.assertNotIn('claim_receipt', review)
        self.assertEqual(self.store.inventory(), inventory)
        self.assertEqual(self.store.journals(), journals)
        self.assertEqual(self.store.get('known_targets'), known)
        await self.assert_no_job()

    async def control_action(self):
        response = await self.client.get('/api/snapshot', headers=self.headers)
        self.assertEqual(response.status, 200)
        controller = next(row for row in (await response.json())['controllers'] if row['device_id'] == 40001)
        return controller['actions']['configure_control']

    async def test_dimmer_editor_stays_available_during_status_read_and_preview_gets_priority(self):
        reading = asyncio.Event()

        async def poll_light():
            async with self.runtime._background_read() as admitted:
                self.assertTrue(admitted)
                async with self.runtime.operation:
                    reading.set()
                    await self.runtime._foreground_pending.wait()

        poll = asyncio.create_task(poll_light())
        try:
            await asyncio.wait_for(reading.wait(), 1)
            self.assertEqual(await self.control_action(), {'enabled': True, 'reason': 'available'})
            self.assertEqual((await asyncio.wait_for(self.preview(), 2)).status, 200)
        finally:
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
        await self.assert_no_job()

    async def test_dimmer_editor_blocks_foreground_reservation_and_unrelated_lock_holder(self):
        busy = {'enabled': False, 'reason': 'operation_in_progress'}
        async with self.runtime.foreground_request():
            self.assertEqual(await self.control_action(), busy)
        async with self.runtime.operation:
            self.assertEqual(await self.control_action(), busy)
            # A background task being scheduled does not make someone else's lock preemptible.
            async def queued_read():
                async with self.runtime._background_read():
                    return await self.control_action()
            self.assertEqual(await asyncio.create_task(queued_read()), busy)
        self.assertEqual(await self.control_action(), {'enabled': True, 'reason': 'available'})

    async def test_exact_preview_fields_and_mode_specific_group_requirements(self):
        invalid = [[], {}, {'target': 40001}, {'target': True, 'mode': 'ha'},
            {'target': 40002, 'mode': 'ha'}, {'target': 40099, 'mode': 'ha'},
            {'target': 40001, 'mode': 'unknown'}, {'target': 40001, 'mode': 'native'},
            {'target': 40001, 'mode': 'native', 'group': True},
            {'target': 40001, 'mode': 'ha', 'group': None},
            {'target': 40001, 'mode': 'ha', 'group': 24575},
            {'target': 40001, 'mode': 'ha', 'extra': True}]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual((await self.preview(body)).status, 400)
        await self.assert_no_job()

    async def test_configure_requires_exact_reviewed_baseline_and_revision(self):
        body = await self.reviewed()
        invalid = [[], {}, body | {'extra': True}, body | {'revision': 'stale'},
            body | {'baseline': []}, body | {'baseline': body['baseline'] | {'extra': True}},
            body | {'baseline': body['baseline'] | {'target': True}},
            {key: value for key, value in body.items() if key != 'baseline'},
            {key: value for key, value in body.items() if key != 'revision'},
            {key: value for key, value in body.items() if key != 'group'},
            body | {'mode': 'ha'}, body | {'group': '293'}]
        with patch('avionmesh_pi.server.DimmerEditor.configure') as configure:
            for candidate in invalid:
                with self.subTest(candidate=candidate):
                    self.assertEqual((await self.configure(candidate)).status, 400)
            configure.assert_not_called()
        await self.assert_no_job()

    async def test_changed_baseline_revision_or_membership_refuses_before_job(self):
        original = await self.reviewed()
        cases = [original | {'revision': '0' * 64},
                 original | {'baseline': original['baseline'] | {'target_generation': 2}}]
        with patch('avionmesh_pi.server.DimmerEditor.configure') as configure:
            for body in cases:
                response = await self.configure(body)
                self.assertEqual(response.status, 409)
                refusal = await response.json()
                self.assertEqual(refusal['error'], 'inventory_changed')
                self.assertFalse(refusal['operation_started'])
                self.assertFalse(refusal['requires_review'])
            inventory = self.store.inventory()
            inventory['groups'][-1]['members'] = []
            self.store.set('inventory', inventory)
            response = await self.configure(original)
            self.assertEqual(response.status, 409)
            self.assertFalse((await response.json())['operation_started'])
            configure.assert_not_called()
        await self.assert_no_job()

    async def test_mesh_offline_busy_and_pending_are_explicit_not_started(self):
        body = await self.reviewed()
        self.runtime.mesh_ready = False
        response = await self.configure(body)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())['error'], 'mesh_offline')
        self.runtime.mesh_ready = True
        async with self.runtime.operation:
            response = await self.configure(body)
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())['error'], 'mesh_busy')
        self.store.begin('dimmer_edit', 40001, {})
        response = await self.configure(body)
        self.assertEqual(response.status, 409)
        refusal = await response.json()
        self.assertEqual(refusal['error'], 'pending_operation')
        self.assertEqual((refusal['operation_started'], refusal['requires_review']), (False, True))
        await self.assert_no_job()

    async def test_model_capability_gates_preview_and_configure_independently(self):
        body = await self.reviewed()
        self.runtime.capabilities['dimmer_editor'] = False
        for response in (await self.preview(), await self.configure(body)):
            self.assertEqual(response.status, 501)
            self.assertEqual((await response.json())['error'], 'dimmer_editor_disabled')
        inventory = self.store.inventory()
        inventory['devices'][0]['product_id'] = 81
        self.store.set('inventory', inventory)
        reload_inventory(self.runtime)
        self.runtime.capabilities['dimmer_editor'] = True
        for response in (await self.preview(), await self.configure(body)):
            self.assertEqual(response.status, 501)
            self.assertEqual((await response.json())['error'], 'anyplace_editor_disabled')
        await self.assert_no_job()

    async def test_accepted_native_and_ha_jobs_forward_exact_review(self):
        for mode in ('native', 'ha'):
            body = await self.reviewed(mode)
            with patch('avionmesh_pi.server.DimmerEditor.configure',
                       return_value={'desired_behavior_observed': True, 'acceptance_required': True}) as configure:
                response = await self.configure(body)
                self.assertEqual(response.status, 202)
                accepted = await response.json()
                self.assertFalse(accepted['completed'])
                await asyncio.sleep(0)
                configure.assert_awaited_once_with(40001, mode, body.get('group'),
                    expected_baseline=body['baseline'], expected_revision=body['revision'])
                status = await (await self.client.get('/api/status', headers=self.headers)).json()
                self.assertEqual(status['jobs']['40001']['id'], accepted['job_id'])
                self.assertEqual(status['jobs']['40001']['state'], 'completed')
        self.assertEqual(self.transport.frames, [])

    async def test_backend_scheduled_admission_failure_remains_known_not_started(self):
        body = await self.reviewed()
        with patch('avionmesh_pi.server.DimmerEditor.configure',
                   side_effect=OperationNotStarted('inventory_changed')) as configure:
            response = await self.configure(body)
            self.assertEqual(response.status, 202)
            await asyncio.sleep(0)
            configure.assert_awaited_once()
            status = await (await self.client.get('/api/status', headers=self.headers)).json()
        result = status['jobs']['40001']
        self.assertEqual((result['state'], result['error']), ('not_started', 'inventory_changed'))
        self.assertFalse(result['operation_started'])
        self.assertFalse(result['retry_allowed'])
        self.assertEqual(self.transport.frames, [])

    async def test_failed_job_preserves_only_its_matching_managed_receipt_identity(self):
        body = await self.reviewed()
        async def failure(*args, **kwargs):
            identifier = self.store.begin('dimmer_edit', 40001, {'control_schema': 'dimmer-control-v1',
                'mode': 'native', 'reservation': {'group_id': 293}})
            self.store.transition(identifier, 'prepared', 'attempted')
            self.store.transition(identifier, 'attempted', 'uncertain')
            error = asyncio.TimeoutError('Private diagnostic must not be serialized')
            error.operation_result = {'transaction': identifier, 'mode': 'native', 'group': 293}
            raise error
        with patch('avionmesh_pi.server.DimmerEditor.configure', side_effect=failure):
            response = await self.configure(body)
            self.assertEqual(response.status, 202)
            job_id = (await response.json())['job_id']
            await asyncio.sleep(0)
            status = await (await self.client.get('/api/status', headers=self.headers)).json()
        job = status['jobs']['40001']
        self.assertEqual(job, {'id': job_id, 'state': 'needs_review', 'result': {
            'transaction': self.store.journals()[-1]['id'], 'mode': 'native', 'group': 293}})
        self.assertEqual(self.transport.frames, [])

    async def test_failed_job_never_infers_an_unrelated_receipt(self):
        body = await self.reviewed()
        error = RuntimeError('private')
        error.operation_result = {'transaction': self.claim, 'mode': 'native', 'group': 293}
        with patch('avionmesh_pi.server.DimmerEditor.configure', side_effect=error):
            response = await self.configure(body)
            self.assertEqual(response.status, 202)
            await asyncio.sleep(0)
            status = await (await self.client.get('/api/status', headers=self.headers)).json()
        self.assertEqual(status['jobs']['40001']['state'], 'needs_review')
        self.assertNotIn('result', status['jobs']['40001'])

    async def test_real_backend_rejects_inventory_race_after_http_preflight_as_not_started(self):
        from avionmesh_pi.dimmer import DimmerEditor
        body = await self.reviewed()
        original = DimmerEditor.preview_configuration
        journals = copy.deepcopy(self.store.journals())
        def changed_after_preview(editor, *args, **kwargs):
            result = original(editor, *args, **kwargs)
            inventory = self.store.inventory()
            inventory['groups'][-1]['members'] = []
            self.store.set('inventory', inventory)
            return result
        with patch('avionmesh_pi.server.DimmerEditor.preview_configuration',
                   autospec=True, side_effect=changed_after_preview):
            response = await self.configure(body)
            self.assertEqual(response.status, 202)
            await asyncio.sleep(0)
            status = await (await self.client.get('/api/status', headers=self.headers)).json()
        result = status['jobs']['40001']
        self.assertEqual((result['state'], result['error']), ('not_started', 'inventory_changed'))
        self.assertFalse(result['operation_started'])
        self.assertFalse(result['requires_review'])
        self.assertEqual(self.store.journals(), journals)
        self.assertEqual(self.transport.frames, [])

    async def test_stale_or_unowned_source_preview_cannot_authorize_writes(self):
        body = await self.reviewed()
        self.runtime.native_observations.clear()
        self.assertEqual((await self.preview()).status, 400)
        response = await self.configure(body)
        self.assertEqual(response.status, 409)
        self.assertFalse((await response.json())['operation_started'])
        self.observe_source()
        self.runtime.native_observations[40001][0]['target'] = 292
        self.assertEqual((await self.preview()).status, 400)
        await self.assert_no_job()
