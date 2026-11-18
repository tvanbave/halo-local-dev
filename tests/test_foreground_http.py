"""Synthetic HTTP requests take priority over an in-flight observation read."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_http as http
import test_membership_http as membership_http
from avionmesh_pi.protocol import decrypt_mcp
from test_service import KEY


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in container')
class ForegroundHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = membership_http.MembershipHttpTests.asyncSetUp
    asyncTearDown = membership_http.MembershipHttpTests.asyncTearDown

    async def hold_observation(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        sent, release = asyncio.Event(), asyncio.Event()
        original = self.transport.send

        async def send(frame):
            packet = decrypt_mcp(frame, KEY)
            if packet.payload[:2] == b'\x01\x0a' and not sent.is_set():
                sent.set()
                await release.wait()
            return await original(frame)

        self.transport.send = send
        worker = asyncio.create_task(self.runtime.observation_refresh(settle=0, gap=.01))
        await asyncio.wait_for(sent.wait(), 1)
        return release, worker

    async def submit_membership(self):
        return await self.client.post('/api/rooms/membership', headers=self.headers,
            json={'targets': [40002], 'group': 291, 'member': False})

    async def stop_worker(self, release, worker):
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    async def test_single_http_membership_waits_for_background_read_and_diagnostics_remain_usable(self):
        release, worker = await self.hold_observation()
        request = asyncio.create_task(self.submit_membership())
        try:
            # Give the original HTTP request time to reach the admission guard.
            await asyncio.sleep(.05)
            response = await asyncio.wait_for(self.client.get('/api/status', headers=self.headers), 1)
            self.assertEqual(response.status, 200)
            capture = await asyncio.wait_for(self.client.post('/api/capture', headers=self.headers,
                json={'target': 40002, 'seconds': 10}), 1)
            self.assertEqual(capture.status, 200)
            identifier = (await capture.json())['id']
            stopped = await asyncio.wait_for(self.client.post('/api/capture/action', headers=self.headers,
                json={'id': identifier, 'action': 'stop'}), 1)
            self.assertEqual(stopped.status, 200)
            self.assertEqual(self.transport.mutations, [])
            release.set()
            response = await asyncio.wait_for(request, 5)
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['physical_memberships_verified'])
            self.assertEqual(self.transport.mutations, [(40002, 291, False)])
            self.assertEqual(self.store.journals(pending=True), [])
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await self.stop_worker(release, worker)

    async def test_inventory_change_during_background_wait_refuses_before_membership_io(self):
        release, worker = await self.hold_observation()
        request = asyncio.create_task(self.submit_membership())
        try:
            for _ in range(100):
                if self.runtime.status()['foreground_waiters']:
                    break
                await asyncio.sleep(.001)
            self.assertEqual(self.runtime.status()['foreground_waiters'], 1)
            inventory = self.store.inventory()
            inventory['groups'][0]['name'] = 'Changed while waiting'
            self.store.set('inventory', inventory)
            release.set()
            response = await asyncio.wait_for(request, 1)
            body = await response.json()
            self.assertEqual(response.status, 409)
            self.assertEqual(body['error'], 'inventory_changed')
            self.assertFalse(body['operation_started'])
            self.assertFalse(body['requires_review'])
            self.assertEqual(self.transport.mutations, [])
            self.assertEqual(self.store.journals(), [])
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await self.stop_worker(release, worker)

    async def test_busy_offline_and_pending_have_distinct_no_start_evidence(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        async with self.runtime.operation:
            with patch('avionmesh_pi.membership._ADMISSION_WAIT_SECONDS', .01):
                response = await self.submit_membership()
            body = await response.json()
            self.assertEqual((response.status, body['error']), (409, 'mesh_busy'))
            self.assertFalse(body['operation_started'])
            self.assertFalse(body['requires_review'])
        self.runtime.mesh_ready = False
        response = await self.submit_membership()
        self.assertEqual((await response.json())['error'], 'mesh_offline')
        self.runtime.mesh_ready = True
        self.store.begin('claim', 40001, {'uuid': '00' * 16})
        response = await self.submit_membership()
        body = await response.json()
        self.assertEqual((response.status, body['error']), (409, 'pending_operation'))
        self.assertFalse(body['operation_started'])
        self.assertTrue(body['requires_review'])
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(len(self.store.journals()), 1)

    async def test_generic_failure_does_not_claim_that_operation_never_started(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        with patch('avionmesh_pi.rooms.Rooms.change', new=AsyncMock(side_effect=RuntimeError('secret'))):
            response = await self.submit_membership()
        self.assertEqual(response.status, 409)
        self.assertEqual(await response.json(), {'error': 'operation_unavailable_or_uncertain'})

    async def test_background_admission_timeout_does_not_invoke_handler(self):
        from contextlib import asynccontextmanager
        from avionmesh_pi.errors import OperationNotStarted

        @asynccontextmanager
        async def unavailable():
            raise OperationNotStarted('background_read_busy')
            yield

        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        with patch.object(self.runtime, 'foreground_request', unavailable), \
                patch('avionmesh_pi.rooms.Rooms.change', new=AsyncMock()) as change:
            response = await self.submit_membership()
        self.assertEqual(response.status, 409)
        body = await response.json()
        self.assertEqual(body['error'], 'background_read_busy')
        self.assertFalse(body['operation_started'])
        change.assert_not_awaited()
        self.assertEqual(self.transport.frames, [])
