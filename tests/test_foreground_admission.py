"""Original requests take priority over read-only refresh without radio cancellation."""
import asyncio
import unittest
from unittest.mock import patch

from avionmesh_pi.errors import OperationNotStarted
import test_observation_refresh as observations


class ForegroundAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await observations.ObservationRefreshTests.asyncSetUp(self)
        self.tasks = []
        self.mesh.silent.update(range(40001, 40011))

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.runtime._foreground_requests, 0)
        self.assertEqual(self.runtime._foreground_waiters, 0)
        self.assertIsNone(self.runtime._background_read_task)
        self.assertFalse(self.runtime.operation.locked())
        await observations.ObservationRefreshTests.asyncTearDown(self)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def until(self, predicate):
        for _ in range(80):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('Expected asynchronous boundary was not reached')

    async def background_light(self, *, startup=False):
        worker = self.task(self.runtime.startup_read(settle=0, gap=0, busy_wait=0) if startup else
            self.runtime.observation_refresh(settle=0, gap=0))
        await self.until(lambda: bool(self.runtime.waiters))
        self.assertTrue(self.runtime.status()['background_read_active'])
        return worker

    async def test_original_handler_runs_once_after_current_field_and_holds_priority(self):
        worker = await self.background_light()
        release, entered = asyncio.Event(), asyncio.Event()
        calls = []
        async def request():
            async with self.runtime.foreground_request():
                calls.append('handler')
                self.assertFalse(self.runtime.operation.locked())
                async with self.runtime.operation:
                    entered.set()
                    await release.wait()
        foreground = self.task(request())
        await self.until(lambda: self.runtime.status()['foreground_waiters'] == 1)
        self.assertEqual(calls, [])
        self.assertEqual(len(self.mesh.frames), 1)
        self.mesh.reply(40001, b'\x01\x0a\x00\x80')
        await self.until(entered.is_set)
        # The reply is retained, but the second field yields to the original request.
        self.assertEqual(self.runtime.device_states()['40001']['brightness']['value'], 128)
        self.assertEqual(len(self.mesh.frames), 1)
        self.assertFalse(self.runtime.status()['background_read_active'])
        self.assertEqual(self.runtime.status()['foreground_requests'], 1)
        for _ in range(8):
            await asyncio.sleep(0)
        self.assertEqual(len(self.mesh.frames), 1)
        release.set()
        await foreground
        await self.until(lambda: len(self.mesh.frames) == 2)
        self.assertEqual(calls, ['handler'])
        self.assertEqual(self.mesh.frames[1][1].mcp_address, 40002)
        worker.cancel()

    async def test_waiting_request_cancellation_does_not_cancel_background_exchange(self):
        await self.background_light()
        handler = []
        async def request():
            async with self.runtime.foreground_request():
                handler.append(True)
        foreground = self.task(request())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        waiter = self.runtime.waiters[0][1]
        foreground.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await foreground
        self.assertEqual(handler, [])
        self.assertFalse(waiter.done())
        self.assertTrue(self.runtime.status()['background_read_active'])
        self.assertEqual(self.runtime.status()['foreground_requests'], 0)

    async def test_bounded_admission_timeout_never_enters_handler_or_cancels_read(self):
        await self.background_light()
        waiter = self.runtime.waiters[0][1]
        handler = []
        async def timeout(awaitable, seconds):
            awaitable.close()
            self.assertEqual(seconds, 20)
            raise asyncio.TimeoutError()
        with patch('avionmesh_pi.runtime.asyncio.wait_for', side_effect=timeout):
            with self.assertRaises(OperationNotStarted) as caught:
                async with self.runtime.foreground_request():
                    handler.append(True)
        self.assertEqual(caught.exception.code, 'background_read_busy')
        self.assertFalse(caught.exception.response()['operation_started'])
        self.assertEqual(handler, [])
        self.assertFalse(waiter.done())
        self.assertTrue(self.runtime.status()['background_read_active'])
        self.assertEqual(self.runtime.status()['foreground_requests'], 0)

    async def test_session_change_while_waiting_refuses_before_handler(self):
        await self.background_light()
        calls = []
        async def request():
            async with self.runtime.foreground_request():
                calls.append(True)
        foreground = self.task(request())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.runtime.session()
        with self.assertRaises(OperationNotStarted) as caught:
            await foreground
        self.assertEqual(caught.exception.code, 'session_changed')
        self.assertEqual(calls, [])
        self.assertEqual(self.store.journals(), [])

    async def test_existing_foreground_contention_is_not_queued(self):
        async with self.runtime.operation:
            async with self.runtime.foreground_request():
                self.assertEqual(self.runtime._foreground_waiters, 0)
                with self.assertRaisesRegex(RuntimeError, 'Another mesh operation'):
                    await self.runtime.read_light(40001)
        self.assertEqual(self.mesh.frames, [])

    async def test_reservation_before_worker_start_prevents_new_background_read(self):
        async with self.runtime.foreground_request():
            self.task(self.runtime.observation_refresh(settle=0, gap=0))
            await self.until(lambda: self.runtime.observation_refresh_progress is not None)
            self.assertEqual(self.runtime.observation_refresh_progress['state'], 'waiting_foreground')
            self.assertEqual(self.mesh.frames, [])
        await self.until(lambda: len(self.mesh.frames) == 1)

    async def test_nested_reservations_release_after_handler_cancellation(self):
        entered = asyncio.Event()
        async def request():
            async with self.runtime.foreground_request():
                async with self.runtime.foreground_request():
                    self.assertEqual(self.runtime._foreground_requests, 2)
                    entered.set()
                    await asyncio.Future()
        foreground = self.task(request())
        await self.until(entered.is_set)
        foreground.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await foreground
        self.assertEqual(self.runtime._foreground_requests, 0)

    async def test_legacy_startup_read_participates_in_foreground_handoff(self):
        await self.background_light(startup=True)
        entered, release = asyncio.Event(), asyncio.Event()
        async def request():
            async with self.runtime.foreground_request():
                entered.set()
                await release.wait()
        foreground = self.task(request())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.mesh.reply(40001, b'\x01\x0a\x00\x80')
        await self.until(entered.is_set)
        self.assertEqual(len(self.mesh.frames), 1)
        self.assertFalse(self.runtime.status()['background_read_active'])
        release.set()
        await foreground

    async def test_local_controller_probe_participates_in_foreground_handoff(self):
        self.runtime.core.controllers.local_modes = {40100}
        self.mesh.silent.add(40100)
        probe = self.task(self.runtime._probe_local_controllers(self.runtime.generation, {40100: (0, 0)}))
        await self.until(lambda: bool(self.runtime.waiters))
        entered = []
        async def request():
            async with self.runtime.foreground_request():
                entered.append(True)
                self.assertIsNone(self.runtime.local_baseline_query)
                self.assertFalse(self.runtime.operation.locked())
        foreground = self.task(request())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        token = self.runtime.local_baseline_query[1]
        self.mesh.reply(40100, bytes([1, 10, token, 128]))
        await foreground
        await probe
        self.assertEqual(entered, [True])
        self.assertEqual(self.runtime.controller_events, {})


if __name__ == '__main__':
    unittest.main()
