"""HTTP admission for the reviewed multi-button keypad save; fake transport only."""
import asyncio
import unittest
from unittest.mock import patch

import test_http as http
from test_controller_workflow import table


@unittest.skipIf(http.create_app is None, 'HTTP dependencies run in isolated container')
class KeypadSaveHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    def arm(self):
        self.binding = table((65535, 500, 501, 502))
        self.runtime.core.controllers.bindings[40001] = self.binding
        self.runtime.capabilities['wired_editor'] = True
        self.runtime.mesh_ready = True

    async def post(self, body):
        return await self.client.post('/api/keypad/save', headers=self.headers, json=body)

    def body(self, **changes):
        data = {'target': 40001, 'revision': self.binding['revision'],
                'changes': [{'slot': 1, 'mode': 'native', 'group': 291}]}
        data.update(changes)
        return data

    async def test_disabled_editor_and_feature_flag(self):
        self.binding = table((65535, 500, 501, 502))
        self.runtime.core.controllers.bindings[40001] = self.binding
        response = await self.post(self.body())
        self.assertEqual(response.status, 501)
        features = (await (await self.client.get('/api/capabilities', headers=self.headers)).json())['features']
        self.assertFalse(features['keypad_edit'])
        self.runtime.capabilities['wired_editor'] = True
        features = (await (await self.client.get('/api/capabilities', headers=self.headers)).json())['features']
        self.assertTrue(features['keypad_edit'])
        self.assertEqual(self.transport.frames, [])

    async def test_invalid_or_stale_sets_are_refused_without_a_job(self):
        self.arm()
        bad = (self.body(revision='stale'), self.body(target=40002), self.body(changes=[]),
               self.body(changes=[{'slot': 1, 'mode': 'native', 'group': 292}]),
               self.body(changes=[{'slot': 1, 'mode': 'native', 'group': 291, 'brightness': 0}]),
               self.body(changes=[{'slot': 1, 'mode': 'ha'}]),
               self.body(extra=True), {'target': 40001})
        with patch('avionmesh_pi.server.KeypadService.apply_many') as apply_many:
            for body in bad:
                with self.subTest(body=body):
                    self.assertEqual((await self.post(body)).status, 400)
            apply_many.assert_not_called()
        status = await (await self.client.get('/api/status', headers=self.headers)).json()
        self.assertNotIn('40001', status['jobs'])
        self.assertEqual(self.transport.frames, [])

    async def test_pending_receipt_or_offline_mesh_blocks_admission(self):
        self.arm()
        self.runtime.mesh_ready = False
        response = await self.post(self.body())
        self.assertEqual(response.status, 409)
        refusal = await response.json()
        self.assertEqual((refusal['error'], refusal['operation_started'], refusal['requires_review']),
                         ('mesh_offline', False, False))
        self.runtime.mesh_ready = True
        self.store.begin('keypad_edit', 40001, {'stages': []})
        response = await self.post(self.body())
        self.assertEqual(response.status, 409)
        refusal = await response.json()
        self.assertEqual((refusal['error'], refusal['operation_started'], refusal['requires_review']),
                         ('pending_operation', False, True))
        self.assertEqual(self.transport.frames, [])

    async def test_accepted_save_runs_as_one_job_with_reviewed_arguments(self):
        self.arm()
        release, calls = asyncio.Event(), []

        async def apply_many(target, changes, revision, *, allowed_models=()):
            calls.append((target, changes, revision, allowed_models))
            await release.wait()
            return {'changed': True, 'verified': True, 'buttons': [1], 'revision': 'next'}
        with patch('avionmesh_pi.server.KeypadService.apply_many', side_effect=apply_many):
            response = await self.post(self.body())
            accepted = await response.json()
            self.assertEqual(response.status, 202)
            self.assertEqual((await self.post(self.body())).status, 409)
            release.set()
            await asyncio.sleep(0)
            status = await (await self.client.get('/api/status', headers=self.headers)).json()
        self.assertEqual(status['jobs']['40001']['id'], accepted['job_id'])
        self.assertEqual(status['jobs']['40001']['state'], 'completed')
        self.assertEqual(status['keypad_saves'], {})
        self.assertEqual(calls, [(40001, [{'slot': 1, 'mode': 'native', 'group': 291}],
                                  self.binding['revision'], (127,))])
        self.assertEqual(self.transport.frames, [])

    async def test_ha_mode_uses_editor_gate_and_leaves_sharing_and_onboarding_unchanged(self):
        self.arm()
        self.runtime.capabilities['inventory_complete'] = True
        before = self.store.inventory()
        changes = [{'slot': 1, 'mode': 'ha'}]
        with patch('avionmesh_pi.server.KeypadService.apply_many', return_value={'verified': True}) as apply_many:
            response = await self.post(self.body(changes=changes))
            self.assertEqual(response.status, 202)
            await asyncio.sleep(0)
            apply_many.assert_awaited_once_with(40001, changes, self.binding['revision'], allowed_models=(127,))
        self.assertFalse(self.runtime.capabilities.get('wired_onboarding', False))
        self.assertEqual(self.store.inventory(), before)

    async def test_ha_preview_does_not_reserve_or_program_and_rejects_caller_selected_target(self):
        self.arm()
        self.runtime.capabilities['inventory_complete'] = True
        before = self.store.inventory()
        body = {'target': 40001, 'slot': 1, 'mode': 'ha', 'revision': self.binding['revision']}
        response = await self.client.post('/api/keypad/preview', headers=self.headers, json=body)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['executable'])
        response = await self.post(self.body(changes=[{'slot': 1, 'mode': 'ha', 'group': 24575}]))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.store.inventory(), before)
        self.assertEqual(self.store.journals(), [])
        self.assertEqual(self.transport.frames, [])


if __name__ == '__main__':
    unittest.main()
