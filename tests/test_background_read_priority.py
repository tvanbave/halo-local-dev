"""Only background observation waits yield; radio writes and explicit reads finish."""
import asyncio
import unittest
from unittest.mock import patch

from avionmesh_pi.protocol import model_command
import test_foreground_admission as fixtures


class BackgroundReadPriorityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ForegroundAdmissionTests.asyncSetUp
    asyncTearDown = fixtures.ForegroundAdmissionTests.asyncTearDown
    task = fixtures.ForegroundAdmissionTests.task
    until = fixtures.ForegroundAdmissionTests.until
    background_light = fixtures.ForegroundAdmissionTests.background_light

    async def test_silent_periodic_reply_does_not_delay_control_and_late_reply_is_observed(self):
        await self.background_light()
        async def request():
            async with self.runtime.foreground_request():
                self.assertFalse(self.runtime.operation.locked())
                self.assertEqual(self.runtime.waiters, [])
                await self.runtime.control(40001, {'state': 'OFF'}, local=True, confirm=False)
                self.mesh.reply(40001, b'\x01\x0a\x00\x00')
                self.assertEqual(self.runtime.device_states()['40001']['brightness']['value'], 0)
                self.assertEqual([p.payload[:2] for _, p in self.mesh.frames],
                                 [b'\x01\x0a', b'\x00\x0a'])
        await asyncio.wait_for(request(), .5)
        self.assertFalse(self.runtime._foreground_pending.is_set())

    async def test_pending_read_transmission_finishes_before_reply_wait_is_abandoned(self):
        sent, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.mesh.send
        cancelled = []
        async def blocked_send(frame):
            sent.set()
            try:
                await release.wait()
                await original(frame)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        self.mesh.send = blocked_send
        self.task(self.runtime.observation_refresh(settle=0, gap=0))
        await sent.wait()
        async def request():
            async with self.runtime.foreground_request():
                self.assertEqual(len(self.mesh.frames), 1)
                self.assertEqual(self.runtime.waiters, [])
                self.assertFalse(self.runtime.operation.locked())
                entered.set()
        foreground = self.task(request())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        self.assertEqual(cancelled, [])
        release.set()
        await asyncio.wait_for(foreground, .5)
        self.assertTrue(entered.is_set())
        self.assertEqual(cancelled, [])

    async def test_background_field_gap_yields_without_sending_second_read(self):
        self.mesh.silent.clear()
        in_gap = asyncio.Event()
        original = self.runtime._light_read_gap
        async def gap(delay):
            in_gap.set()
            await original(delay)
        async def background():
            async with self.runtime._background_read():
                return await self.runtime.read_light(40001, field_gap=10)
        with patch.object(self.runtime, '_light_read_gap', side_effect=gap):
            worker = self.task(background())
            await in_gap.wait()
            async def request():
                async with self.runtime.foreground_request():
                    self.assertFalse(self.runtime.operation.locked())
                    self.assertEqual(len(self.mesh.frames), 1)
            await asyncio.wait_for(request(), .5)
            result = await worker
        self.assertEqual(result['observed']['brightness'], 128)
        self.assertFalse(result['complete'])

    async def test_targeted_feedback_gap_yields_without_second_field(self):
        in_gap = asyncio.Event()
        original = self.runtime._light_read_gap
        async def gap(delay):
            in_gap.set()
            await original(10)
        async def background():
            async with self.runtime._background_read():
                return await self.runtime._request_light_fields(40001, {'brightness', 'color_temp'})
        with patch.object(self.runtime, '_light_read_gap', side_effect=gap):
            worker = self.task(background())
            await in_gap.wait()
            async def request():
                async with self.runtime.foreground_request():
                    self.assertEqual(len(self.mesh.frames), 1)
            await asyncio.wait_for(request(), .5)
            self.assertEqual(await worker, {'brightness'})

    async def test_explicit_read_reply_is_not_abandoned_by_foreground_reservation(self):
        reader = self.task(self.runtime.read_light(40001, brightness_only=True))
        await self.until(lambda: bool(self.runtime.waiters))
        async with self.runtime.foreground_request():
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(reader.done())
            self.assertFalse(self.runtime.waiters[0][1].done())
            self.mesh.reply(40001, b'\x01\x0a\x00\x80')
            self.assertTrue((await reader)['complete'])

    async def test_nonread_exchange_even_in_background_scope_is_not_interrupted(self):
        async def background():
            async with self.runtime._background_read():
                return await self.runtime.exchange(model_command(40001, 0, 10), lambda p: True)
        reader = self.task(background())
        await self.until(lambda: bool(self.runtime.waiters))
        entered = []
        async def request():
            async with self.runtime.foreground_request():
                entered.append(True)
        foreground = self.task(request())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(entered, [])
        self.assertFalse(reader.done())
        self.mesh.reply(40001, b'\x01\x0a\x00\x80')
        await asyncio.wait_for(foreground, .5)
        await reader
        self.assertEqual(len(self.mesh.frames), 1)

    async def test_silent_controller_baseline_yields_without_fake_button_event(self):
        self.runtime.core.controllers.local_modes = {40100}
        self.mesh.silent.add(40100)
        probe = self.task(self.runtime._probe_local_controllers(self.runtime.generation, {40100: (0, 0)}))
        await self.until(lambda: bool(self.runtime.waiters))
        async def request():
            async with self.runtime.foreground_request():
                self.assertIsNone(self.runtime.local_baseline_query)
                self.assertFalse(self.runtime.operation.locked())
        await asyncio.wait_for(request(), .5)
        await probe
        self.assertEqual(self.runtime.controller_events, {})
        self.assertEqual(self.runtime.core.controllers.baselines, {})
        self.assertEqual(len(self.mesh.frames), 1)

    async def test_silent_background_read_without_request_keeps_normal_timeout(self):
        async with self.runtime._background_read():
            with self.assertRaises(asyncio.TimeoutError):
                await self.runtime.exchange(model_command(40001, 1, 10), lambda p: True, timeout=.02)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(len(self.mesh.frames), 1)
