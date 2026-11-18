"""Synthetic HTTP jobs retain foreground priority without crossing sessions."""
import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch

import test_http as http


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in container')
class JobHandoffTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    async def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('Expected job boundary was not reached')

    async def start_read(self):
        response = await self.client.post('/api/keypad/read', headers=self.headers,
            json={'target':40001})
        self.assertEqual(response.status, 202)
        return await response.json()

    async def status(self):
        response = await self.client.get('/api/status', headers=self.headers)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_accepted_job_holds_reservation_across_response_and_until_completion(self):
        release, entered = asyncio.Event(), asyncio.Event()
        counts = []
        async def read(target):
            counts.append(self.runtime._foreground_requests)
            entered.set()
            await release.wait()
            return {'verified':True}
        worker = None
        try:
            with patch('avionmesh_pi.keypad.KeypadService.read', side_effect=read) as read_call:
                accepted = await self.start_read()
                self.assertTrue(entered.is_set())
                # Child entered before the parent HTTP reservation was released.
                self.assertEqual(counts, [2])
                self.assertEqual(self.runtime._foreground_requests, 1)
                worker = asyncio.create_task(self.runtime.observation_refresh(settle=0, gap=0))
                await self.until(lambda:self.runtime.observation_refresh_progress is not None)
                self.assertEqual(self.runtime.observation_refresh_progress['state'], 'waiting_foreground')
                self.assertEqual(self.transport.frames, [])
                running = (await self.status())['jobs']['40001']
                self.assertEqual((running['id'], running['state']), (accepted['job_id'], 'running'))
                release.set()
                await self.until(lambda:self.runtime._foreground_requests == 0)
                completed = (await self.status())['jobs']['40001']
                self.assertEqual((completed['id'], completed['state']), (accepted['job_id'], 'completed'))
                read_call.assert_awaited_once_with(40001)
                await self.until(lambda:bool(self.transport.frames))
        finally:
            release.set()
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(self.runtime._foreground_requests, 0)
        self.assertEqual(self.runtime.waiters, [])
        self.assertFalse(self.runtime.operation.locked())

    async def test_job_cancellation_releases_child_reservation_and_keeps_interrupted_result(self):
        job_tasks = []
        async def read(target):
            job_tasks.append(asyncio.current_task())
            await asyncio.Future()
        with patch('avionmesh_pi.keypad.KeypadService.read', side_effect=read):
            accepted = await self.start_read()
            self.assertEqual(self.runtime._foreground_requests, 1)
            job_tasks[0].cancel()
            await asyncio.gather(job_tasks[0], return_exceptions=True)
        self.assertEqual(self.runtime._foreground_requests, 0)
        self.assertEqual(self.runtime._foreground_waiters, 0)
        self.assertFalse(self.runtime.operation.locked())
        self.assertEqual(self.transport.frames, [])
        job = (await self.status())['jobs']['40001']
        self.assertEqual((job['id'], job['state']), (accepted['job_id'], 'interrupted'))

    async def test_session_change_between_acceptance_and_child_entry_prevents_factory(self):
        original = self.runtime.foreground_request
        entries = []
        @asynccontextmanager
        async def changed_before_child(**kwargs):
            entries.append(True)
            if len(entries) == 2:
                self.runtime.session()
            async with original(**kwargs):
                yield
        with patch.object(self.runtime, 'foreground_request', changed_before_child), \
                patch('avionmesh_pi.keypad.KeypadService.read', new=AsyncMock()) as read:
            accepted = await self.start_read()
            job = (await self.status())['jobs']['40001']
            self.assertEqual(job['id'], accepted['job_id'])
            self.assertEqual((job['state'], job['error']), ('not_started', 'session_changed'))
            self.assertFalse(job['operation_started'])
            self.assertFalse(job['requires_review'])
            read.assert_not_awaited()
        self.assertEqual(self.runtime._foreground_requests, 0)
        self.assertEqual(self.runtime._foreground_waiters, 0)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def test_rejected_job_target_leaves_no_foreground_reservation(self):
        response = await self.client.post('/api/keypad/read', headers=self.headers,
            json={'target':49999})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.runtime._foreground_requests, 0)
        self.assertEqual((await self.status())['jobs'], {})
        self.assertEqual(self.transport.frames, [])
