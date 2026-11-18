import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch
import test_http as http


@unittest.skipIf(http.create_app is None, 'HTTP dependencies tested in container')
class SceneHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    async def wait_job(self):
        for _ in range(50):
            response = await self.client.get('/api/scenes', headers=self.headers)
            state = await response.json()
            if state['job'] and state['job']['state'] != 'running': return state
            await asyncio.sleep(.001)
        self.fail('scene job did not finish')

    async def test_snapshot_capability_and_no_startup_radio_work(self):
        cap = await (await self.client.get('/api/capabilities', headers=self.headers)).json()
        self.assertTrue(cap['features']['scenes'])
        body = await (await self.client.get('/api/snapshot', headers=self.headers)).json()
        self.assertTrue(body['scene_workflow']['enabled'])
        self.assertEqual(body['scene_workflow']['scenes'], [])
        self.assertEqual(self.transport.frames, [])

    async def test_exact_fields_and_auth_reject_before_start(self):
        with patch.object(self.runtime.scene_engine, 'create', new=AsyncMock()) as create:
            reply = await self.client.post('/api/scenes/create', json={'name':'Example','members':[],'exposed':False})
            self.assertEqual(reply.status, 401)
            reply = await self.client.post('/api/scenes/create', headers=self.headers,
                json={'name':'Example','members':[],'exposed':False,'raw':'unsafe'})
            self.assertEqual(reply.status, 400)
            create.assert_not_awaited()

    async def test_async_failure_requires_review_and_does_not_retry(self):
        with patch.object(self.runtime.scene_engine, 'create', new=AsyncMock(
                return_value={'verified':False,'requires_review':True,'transaction':'retained'})) as create:
            reply = await self.client.post('/api/scenes/create', headers=self.headers,
                json={'name':'Example','members':[{'target':40002,'brightness':128,'kelvin':4000}], 'exposed':False})
            self.assertEqual(reply.status, 202)
            state = await self.wait_job()
            self.assertEqual(state['job']['state'], 'failed')
            self.assertTrue(state['job']['result']['requires_review'])
            create.assert_awaited_once()

    async def test_recovery_and_resume_are_separate_explicit_requests(self):
        with patch.object(self.runtime.scene_engine, 'recover', new=AsyncMock(return_value={'read_only':True})) as recover, \
                patch.object(self.runtime.scene_engine, 'resume', new=AsyncMock(return_value={'verified':True})) as resume:
            reply = await self.client.post('/api/scenes/recover', headers=self.headers,json={'transaction':'example'})
            self.assertEqual(reply.status, 202)
            await self.wait_job()
            recover.assert_awaited_once_with('example'); resume.assert_not_awaited()
            reply = await self.client.post('/api/scenes/resume', headers=self.headers,json={'transaction':'example'})
            self.assertEqual(reply.status, 202)
            await self.wait_job(); resume.assert_awaited_once_with('example')

    async def test_second_request_does_not_start_while_scene_job_active(self):
        gate = asyncio.Event()
        async def blocked(*args):
            await gate.wait()
            return {'verified':True}
        with patch.object(self.runtime.scene_engine, 'create', side_effect=blocked) as create:
            request={'name':'Example','members':[{'target':40002,'brightness':128,'kelvin':4000}], 'exposed':False}
            first = await self.client.post('/api/scenes/create',headers=self.headers,json=request)
            second = await self.client.post('/api/scenes/create',headers=self.headers,json=request)
            self.assertEqual(first.status,202);self.assertEqual(second.status,409)
            gate.set(); await self.wait_job(); create.assert_awaited_once()

    async def test_activation_timing_includes_middleware_and_job_foreground_wait(self):
        now = [100]
        self.runtime.clock = lambda: now[0]
        @asynccontextmanager
        async def waiting_foreground():
            now[0] += 50
            yield
        result = {'sent': True, 'timing': {'handler_to_submission_ms': 20,
                  'physical_response_measured': False}}
        with patch.object(self.runtime, 'foreground_request', waiting_foreground), \
                patch.object(self.runtime.scene_engine, 'activate', new=AsyncMock(return_value=result)) as activate:
            response = await self.client.post('/api/scenes/activate', headers=self.headers,
                json={'id': 'a' * 32})
            self.assertEqual(response.status, 202)
            state = await self.wait_job()
        activate.assert_awaited_once_with('a' * 32)
        timing = state['job']['result']['timing']
        self.assertEqual(timing['request_to_handler_ms'], 100)
        self.assertEqual(timing['request_to_submission_ms'], 120)
        self.assertFalse(timing['physical_response_measured'])

    async def test_keypad_comparison_rejects_extra_fields_and_does_not_retry_failure(self):
        with patch.object(self.runtime.scene_engine, 'activate', new=AsyncMock(
                side_effect=RuntimeError('Synthetic uncertain submission'))) as activate:
            body = {'id':'b'*32}
            response = await self.client.post('/api/scenes/activate-keypad-test',
                json=body)
            self.assertEqual(response.status, 401)
            response = await self.client.post('/api/scenes/activate-keypad-test',
                headers=self.headers, json=dict(body, flag=5))
            self.assertEqual(response.status, 400)
            response = await self.client.post('/api/scenes/activate',
                headers=self.headers, json=dict(body, recall_profile='keypad_capture'))
            self.assertEqual(response.status, 400)
            activate.assert_not_awaited()
            response = await self.client.post('/api/scenes/activate-keypad-test',
                headers=self.headers, json=body)
            self.assertEqual(response.status, 202)
            state = await self.wait_job()
            self.assertEqual(state['job']['state'], 'failed')
            activate.assert_awaited_once_with('b'*32, recall_profile='keypad_capture')

    async def test_quiet_comparison_rejects_auth_extra_fields_and_never_waits_or_retries_failure(self):
        with patch.object(self.runtime.scene_engine, 'activate', new=AsyncMock(
                side_effect=RuntimeError('Synthetic uncertain submission'))) as activate:
            body = {'id': 'b' * 32}
            response = await self.client.post('/api/scenes/activate-quiet-test', json=body)
            self.assertEqual(response.status, 401)
            response = await self.client.post('/api/scenes/activate-quiet-test',
                headers=self.headers, json=dict(body, seconds=60))
            self.assertEqual(response.status, 400)
            activate.assert_not_awaited()
            response = await self.client.post('/api/scenes/activate-quiet-test',
                headers=self.headers, json=body)
            self.assertEqual(response.status, 202)
            state = await self.wait_job()
            self.assertEqual(state['job']['state'], 'failed')
            activate.assert_awaited_once_with('b' * 32)
            self.assertEqual(self.runtime._foreground_requests, 0)

    async def test_quiet_comparison_preserves_submission_timing_and_releases_reservation(self):
        result = {'sent': True, 'timing': {'handler_to_submission_ms': 10,
                  'physical_response_measured': False}}
        with patch('avionmesh_pi.server.SCENE_QUIET_TEST_SECONDS', .02), \
                patch.object(self.runtime.scene_engine, 'activate', new=AsyncMock(return_value=result)) as activate:
            response = await self.client.post('/api/scenes/activate-quiet-test',
                headers=self.headers, json={'id': 'a' * 32})
            self.assertEqual(response.status, 202)
            self.assertGreater(self.runtime._foreground_requests, 0)
            async with self.runtime._background_read() as admitted:
                self.assertFalse(admitted)
            state = await self.wait_job()
        self.assertEqual(state['job']['state'], 'complete')
        self.assertEqual(state['job']['result']['background_read_quiet_ms'], 20)
        timing = state['job']['result']['timing']
        self.assertEqual(timing['request_to_submission_ms'], timing['request_to_handler_ms'] + 10)
        self.assertFalse(timing['physical_response_measured'])
        self.assertEqual(self.runtime._foreground_requests, 0)
        activate.assert_awaited_once_with('a' * 32)

    async def test_shutdown_during_quiet_comparison_releases_priority_without_recall_retry(self):
        with patch('avionmesh_pi.server.SCENE_QUIET_TEST_SECONDS', 60), \
                patch.object(self.runtime.scene_engine, 'activate', new=AsyncMock(return_value={'sent': True})) as activate:
            response = await self.client.post('/api/scenes/activate-quiet-test',
                headers=self.headers, json={'id': 'c' * 32})
            self.assertEqual(response.status, 202)
            self.assertGreater(self.runtime._foreground_requests, 0)
            await self.client.close()
            self.assertEqual(self.runtime._foreground_requests, 0)
            self.assertFalse(self.runtime._foreground_pending.is_set())
            activate.assert_awaited_once_with('c' * 32)

    # ---- owner exits, unchecked lights and failure containment ----
    async def test_unchecked_lights_are_validated_and_passed_only_when_named(self):
        with patch.object(self.runtime.scene_engine, 'verify', new=AsyncMock(return_value={'verified': True})) as verify, \
                patch.object(self.runtime.scene_engine, 'recover', new=AsyncMock(return_value={'verified': True})) as recover:
            for bad in ('40002', [40002, 40002], [12], [True], list(range(40000, 40070))):
                reply = await self.client.post('/api/scenes/verify', headers=self.headers, json={'id': 'a' * 32, 'skip': bad})
                self.assertEqual(reply.status, 400)
            reply = await self.client.post('/api/scenes/activate', headers=self.headers, json={'id': 'a' * 32, 'skip': [40002]})
            self.assertEqual(reply.status, 400)       # a recall never takes this field
            reply = await self.client.post('/api/scenes/resume', headers=self.headers, json={'transaction': 'example', 'skip': [40002]})
            self.assertEqual(reply.status, 400)       # a step that writes never takes it either
            verify.assert_not_awaited()
            reply = await self.client.post('/api/scenes/verify', headers=self.headers, json={'id': 'a' * 32, 'skip': [40002]})
            self.assertEqual(reply.status, 202); await self.wait_job()
            verify.assert_awaited_once_with('a' * 32, skip=[40002])
            reply = await self.client.post('/api/scenes/recover', headers=self.headers, json={'transaction': 'example', 'skip': []})
            self.assertEqual(reply.status, 202); await self.wait_job()
            recover.assert_awaited_once_with('example')

    async def test_silent_light_is_named_in_the_failed_job(self):
        from avionmesh_pi.scene_engine import SceneLightSilent
        with patch.object(self.runtime.scene_engine, 'verify', new=AsyncMock(side_effect=SceneLightSilent(40002))) as verify:
            reply = await self.client.post('/api/scenes/verify', headers=self.headers, json={'id': 'a' * 32})
            self.assertEqual(reply.status, 202)
            state = await self.wait_job()
            self.assertEqual(state['job']['state'], 'failed'); self.assertEqual(state['job']['unreachable'], 40002)
            self.assertIn('did not answer', state['job']['error'])
            verify.assert_awaited_once()

    async def test_unexpected_failure_never_leaves_the_scene_job_running(self):
        with patch.object(self.runtime.scene_engine, 'verify', new=AsyncMock(side_effect=KeyError('synthetic defect'))), \
                self.assertLogs('avionmesh_pi.server', level='ERROR'):
            reply = await self.client.post('/api/scenes/verify', headers=self.headers, json={'id': 'a' * 32})
            self.assertEqual(reply.status, 202)
            state = await self.wait_job()
        self.assertEqual(state['job']['state'], 'failed')
        self.assertNotIn('synthetic defect', state['job']['error'])
        with patch.object(self.runtime.scene_engine, 'verify', new=AsyncMock(return_value={'verified': True})):
            reply = await self.client.post('/api/scenes/verify', headers=self.headers, json={'id': 'a' * 32})
            self.assertEqual(reply.status, 202)        # the next request is not locked out
            self.assertEqual((await self.wait_job())['job']['state'], 'complete')

    async def test_set_aside_and_forget_use_no_radio_and_work_offline_or_with_scenes_off(self):
        self.runtime.capabilities['native_scenes'] = False
        self.runtime.mesh_ready = False
        with patch.object(self.runtime.scene_engine, 'set_aside', return_value={'set_aside': True}) as aside, \
                patch.object(self.runtime.scene_engine, 'forget', return_value={'forgotten': True}) as forget:
            reply = await self.client.post('/api/scenes/set-aside', json={'transaction': 'example'})
            self.assertEqual(reply.status, 401)
            reply = await self.client.post('/api/scenes/set-aside', headers=self.headers, json={'transaction': 'example', 'force': True})
            self.assertEqual(reply.status, 400)
            aside.assert_not_called()
            reply = await self.client.post('/api/scenes/set-aside', headers=self.headers, json={'transaction': 'example'})
            self.assertEqual(reply.status, 200); self.assertTrue((await reply.json())['result']['set_aside'])
            aside.assert_called_once_with('example')
            reply = await self.client.post('/api/scenes/forget', headers=self.headers, json={'id': 'a' * 32})
            self.assertEqual(reply.status, 200); forget.assert_called_once_with('a' * 32)
            reply = await self.client.post('/api/scenes/verify', headers=self.headers, json={'id': 'a' * 32})
            self.assertEqual(reply.status, 501)        # everything else stays off
        self.assertEqual(self.transport.frames, [])

    async def test_set_aside_is_refused_while_other_mesh_work_holds_the_lock(self):
        with patch.object(self.runtime.scene_engine, 'set_aside', return_value={'set_aside': True}) as aside:
            async with self.runtime.operation:
                reply = await self.client.post('/api/scenes/set-aside', headers=self.headers, json={'transaction': 'example'})
            self.assertEqual(reply.status, 409); aside.assert_not_called()

    async def test_scenes_switched_off_in_configuration(self):
        self.runtime.capabilities['native_scenes'] = False
        cap = await (await self.client.get('/api/capabilities', headers=self.headers)).json()
        self.assertFalse(cap['features']['scenes'])
        body = await (await self.client.get('/api/snapshot', headers=self.headers)).json()
        self.assertFalse(body['scene_workflow']['enabled'])
        with patch.object(self.runtime.scene_engine, 'create', new=AsyncMock()) as create:
            reply = await self.client.post('/api/scenes/create', headers=self.headers,
                json={'name': 'Example', 'members': [{'target': 40002, 'brightness': 128, 'kelvin': 4000}], 'exposed': False})
            self.assertEqual(reply.status, 501); create.assert_not_awaited()

    async def test_damaged_scene_record_pauses_scenes_but_not_the_page(self):
        from avionmesh_pi.scene_engine import KEY as SCENES
        self.store.set(SCENES, [{'id': 'not a scene'}])
        with self.assertLogs('avionmesh_pi.server', level='ERROR'):
            reply = await self.client.get('/api/snapshot', headers=self.headers)
        self.assertEqual(reply.status, 200)
        body = await reply.json()
        self.assertFalse(body['scene_workflow']['enabled']); self.assertIn('paused', body['scene_workflow']['error'])
        self.assertEqual(body['scene_workflow']['scenes'], [])
        self.assertTrue(body['inventory']['devices'])   # the rest of the page still has its data
        self.runtime.session()                          # publication is skipped, not raised
