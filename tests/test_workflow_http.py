"""Integrated room/controller presentation, jobs and review safety; fake transports only."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import test_http as http
from test_controller_workflow import table


@unittest.skipIf(http.create_app is None, 'HTTP dependencies run in isolated container')
class WorkflowHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    async def test_snapshot_and_sse_share_controller_and_ha_views_without_radio(self):
        response = await self.client.get('/api/snapshot', headers=self.headers)
        snapshot = await response.json()
        self.assertEqual(snapshot['controllers'][0]['device_id'], 40001)
        self.assertFalse(snapshot['controllers'][0]['actions']['edit_assignment']['enabled'])
        self.assertEqual(snapshot['ha_readiness']['ha_acceptance'], 'not_established_by_snapshot')
        stream = await self.client.get('/api/events', headers=self.headers)
        try:
            import json
            block = await asyncio.wait_for(stream.content.readuntil(b'\n\n'), 2)
            data = json.loads(next(line[6:] for line in block.decode().splitlines() if line.startswith('data: ')))
            self.assertEqual(data['controllers'], snapshot['controllers'])
            self.assertEqual(data['ha_readiness'], snapshot['ha_readiness'])
        finally:
            stream.close()

    async def test_idle_stream_heartbeat_and_new_snapshot_after_change(self):
        now = [10]
        timings = self.runtime.command_timings
        timings.clock = lambda: now[0]
        command = timings.start(40002, [40002], {'brightness':100}, source='ui', received_ms=10, shared=False)
        timings.dispatch(command)
        timings.sent(command)
        timings.observe(40002, {'brightness':100}, 10)
        stream = await self.client.get('/api/events', headers=self.headers)
        try:
            first = await asyncio.wait_for(stream.content.readuntil(b'\n\n'), 3)
            self.assertIn(b'event: snapshot', first)
            now[0] += 1000
            idle = await asyncio.wait_for(stream.content.readuntil(b'\n\n'), 3)
            self.assertEqual(idle, b': heartbeat\n\n')
            self.runtime.mesh_ready = not self.runtime.mesh_ready
            changed = await asyncio.wait_for(stream.content.readuntil(b'\n\n'), 3)
            self.assertIn(b'event: snapshot', changed)
        finally:
            stream.close()
        self.assertEqual(self.transport.frames, [])

    async def test_snapshot_and_sse_include_new_light_status_grace(self):
        from avionmesh_pi.lifecycle import reload_inventory
        inv=self.store.inventory();inv['devices'].append({'device_id':40005,'product_id':134,'name':'New bulb','exposed':False})
        self.store.set('inventory',inv);reload_inventory(self.runtime)
        response=await self.client.get('/api/snapshot',headers=self.headers);snapshot=await response.json()
        self.assertEqual(snapshot['initial_light_status'],{'40005':{'remaining_ms':120000}})
        self.assertNotIn('40005',snapshot['device_states'])
        stream=await self.client.get('/api/events',headers=self.headers)
        try:
            import json
            block=await asyncio.wait_for(stream.content.readuntil(b'\n\n'),2)
            data=json.loads(next(line[6:] for line in block.decode().splitlines() if line.startswith('data: ')))
            self.assertEqual(data['initial_light_status'],snapshot['initial_light_status'])
        finally:stream.close()
        self.assertEqual(self.transport.frames,[])

    async def test_snapshot_keeps_keypad_drafts_during_radio_lock_without_admitting_writes(self):
        from avionmesh_pi.lifecycle import reload_inventory
        for model, flag in ((127, 'wired_editor'), (126, 'battery_editor')):
            with self.subTest(model=model):
                data = self.store.inventory()
                next(device for device in data['devices'] if device['device_id'] == 40001)['product_id'] = model
                self.store.import_inventory(data)
                reload_inventory(self.runtime)
                self.runtime.capabilities.update(wired_editor=False, battery_editor=False, inventory_complete=True)
                self.runtime.capabilities[flag] = True
                self.runtime.core.controllers.bindings[40001] = table()
                await self.runtime.operation.acquire()
                try:
                    response = await asyncio.wait_for(self.client.get('/api/snapshot', headers=self.headers), 2)
                    self.assertEqual(response.status, 200)
                    view = next(c for c in (await response.json())['controllers'] if c['device_id'] == 40001)
                    self.assertTrue(view['editor_available'])
                    self.assertTrue(all(row['draft_editable'] and row['ha_draft_available'] for row in view['slots']))
                    self.assertFalse(view['actions']['edit_assignment']['enabled'])
                    self.assertEqual(view['actions']['edit_assignment']['reason'], 'operation_in_progress')
                    self.assertTrue(all(not row['editable'] and not row['ha_available'] for row in view['slots']))
                finally:
                    self.runtime.operation.release()
                response = await self.client.get('/api/snapshot', headers=self.headers)
                view = next(c for c in (await response.json())['controllers'] if c['device_id'] == 40001)
                self.assertTrue(view['editor_available'])
                self.assertTrue(view['actions']['edit_assignment']['enabled'])
                self.assertTrue(all(row['editable'] and row['ha_available'] for row in view['slots']))
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(pending=True), [])

    async def test_controller_job_has_unique_synchronously_published_identity(self):
        release = asyncio.Event()
        async def read(target):
            await release.wait()
            return {'verified': True}
        with patch('avionmesh_pi.server.KeypadService.read', side_effect=read):
            response = await self.client.post('/api/keypad/read', headers=self.headers, json={'target': 40001})
            accepted = await response.json()
            self.assertEqual(response.status, 202)
            self.assertTrue(accepted['job_id'])
            response = await self.client.get('/api/status', headers=self.headers)
            job = (await response.json())['jobs']['40001']
            self.assertEqual(job, {'id': accepted['job_id'], 'state': 'running'})
            response = await self.client.post('/api/keypad/read', headers=self.headers, json={'target': 40001})
            self.assertEqual(response.status, 409)
            release.set()
            await asyncio.sleep(0)
            response = await self.client.get('/api/status', headers=self.headers)
            job = (await response.json())['jobs']['40001']
            self.assertEqual(job['id'], accepted['job_id'])
            self.assertEqual(job['state'], 'completed')
            response = await self.client.post('/api/keypad/read', headers=self.headers, json={'target': 40001})
            next_job = await response.json()
            self.assertNotEqual(next_job['job_id'], accepted['job_id'])
        self.assertEqual(self.transport.frames, [])

    async def test_preview_matches_busy_offline_pending_and_protected_admission(self):
        binding = table((65535, 500, 501, 502))
        self.runtime.core.controllers.bindings[40001] = binding
        self.runtime.capabilities['wired_editor'] = True
        body = {'target': 40001, 'slot': 1, 'mode': 'native', 'group': 291,
                'brightness': 100, 'revision': binding['revision']}
        async def preview(reason, executable=False):
            response = await self.client.post('/api/keypad/preview', headers=self.headers, json=body)
            self.assertEqual(response.status, 200)
            result = await response.json()
            self.assertEqual(result['reason'], reason)
            self.assertEqual(result['executable'], executable)
        await preview('capability_enabled', True)
        self.runtime.mesh_ready = False
        await preview('mesh_offline')
        self.runtime.mesh_ready = True
        await self.runtime.operation.acquire()
        try:
            await preview('operation_in_progress')
        finally:
            self.runtime.operation.release()
        identifier = self.store.begin('keypad_edit', 40001, {'stages': []})
        await preview('resolve_pending_operations')
        self.store.transition(identifier, 'prepared', 'cancelled')
        protected = next(g['group_id'] for g in self.store.inventory()['groups'] if g['protected'])
        binding = table((protected, 500, 501, 502))
        self.runtime.core.controllers.bindings[40001] = binding
        body['revision'] = binding['revision']
        await preview('protected_assignment_requires_lifecycle')
        response = await self.client.post('/api/keypad/save', headers=self.headers,
            json={'target': 40001, 'revision': binding['revision'],
                  'changes': [{'slot': 1, 'mode': 'native', 'group': 291, 'brightness': 100}]})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.store.journals(pending=True), [])
        self.assertEqual(self.transport.frames, [])

    async def test_read_only_snapshot_never_advertises_writers_or_room_programming(self):
        from aiohttp.test_utils import TestClient, TestServer
        self.runtime.capabilities.update(wired_editor=True, group_membership=True, group_membership_readback=True)
        self.runtime.core.controllers.bindings[40001] = table()
        client = TestClient(TestServer(http.create_app(self.runtime, self.token, read_only=True)))
        await client.start_server()
        try:
            response = await client.get('/api/snapshot', headers=self.headers)
            views = (await response.json())['controllers']
            self.assertTrue(all(not action['enabled'] for view in views for action in view['actions'].values()))
            self.assertTrue(all(not view['editor_available'] for view in views))
            self.assertTrue(all(not row['draft_editable'] and not row['ha_draft_available']
                                for view in views for row in view['slots']))
            response = await client.get('/api/rooms', headers=self.headers)
            self.assertFalse((await response.json())['programming_enabled'])
            response = await client.post('/api/rooms/membership', headers=self.headers,
                                         json={'targets': [40002], 'group': 291, 'member': True})
            self.assertEqual(response.status, 403)
        finally:
            await client.close()
        self.assertEqual(self.transport.frames, [])

    async def test_maximum_inventory_snapshot_fits_bounded_live_ui_frame(self):
        from avionmesh_pi.lifecycle import reload_inventory
        data = self.store.inventory()
        data['devices'] = [{'device_id': 40001+i, 'product_id': 127, 'name': 'K'*128, 'exposed': False}
                           for i in range(512)]
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        self.runtime.core.controllers.bindings = {40001+i: table() for i in range(512)}
        response = await self.client.get('/api/snapshot', headers=self.headers)
        self.assertEqual(response.status, 200)
        payload = await response.read()
        self.assertGreater(len(payload), 1024*1024)
        self.assertLess(len(payload), 4*1024*1024)
        import json
        self.assertEqual(len(json.loads(payload)['controllers']), 512)
        self.assertEqual(self.transport.frames, [])
