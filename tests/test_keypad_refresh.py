"""Synthetic wired mapping recovery; no hardware, broker or automatic setters."""
import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from avionmesh_pi.keypad import BackgroundReadDeferred, KeypadService
from avionmesh_pi.keypad_refresh import WiredKeypadRefresh
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_editor import EditorTransport
from test_service import KEY


class WiredKeypadRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 40001, 'product_id': 127, 'name': 'Wired', 'exposed': True},
            {'device_id': 40002, 'product_id': 126, 'name': 'Battery', 'exposed': True},
            {'device_id': 40003, 'product_id': 91, 'name': 'Dimmer', 'exposed': True}],
            'groups': []})
        self.now = 0
        self.transport = EditorTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = True
        self.runtime.capabilities['wired_editor'] = True
        self.runtime.session()
        self.keypad = KeypadService(self.runtime)
        self.refresh = WiredKeypadRefresh(self.runtime, self.keypad)
        self.runtime.keypad_binding_refresher = self.refresh

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def settle(self):
        await self.refresh.tick()
        self.now += 5000

    async def test_two_matching_tables_restore_mapping_without_writes_or_events(self):
        self.runtime.mqtt_connection(True)
        messages = []
        self.runtime.sink = messages.append
        await self.settle()
        self.assertEqual(self.transport.sequence, 1)
        await self.refresh.tick()
        self.assertEqual(self.transport.sequence, 11)
        self.assertEqual(set(self.runtime.core.controllers.bindings), {40001})
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])
        self.assertFalse(any(isinstance(m['payload'], dict) and 'event_type' in m['payload']
                             for m in messages))
        await self.refresh.tick()
        self.assertEqual(self.transport.sequence, 11)
        self.assertEqual(self.refresh.progress['state'], 'ready')

    async def test_manual_read_counts_and_reconnect_rearms(self):
        await self.keypad.read(40001)
        await self.settle()
        await self.refresh.tick()
        self.assertEqual(self.transport.sequence, 11)
        self.runtime.session()
        await self.settle()
        await self.refresh.tick()
        self.assertEqual(self.transport.sequence, 21)
        self.assertEqual(self.transport.writes, [])

    async def test_capability_preview_offline_and_foreground_gates(self):
        await self.settle()
        for attribute, value in (('preview_only', True), ('mesh_ready', False),
                                  ('_foreground_requests', 1)):
            original = getattr(self.runtime, attribute, None)
            setattr(self.runtime, attribute, value)
            await self.refresh.tick()
            setattr(self.runtime, attribute, original)
        self.runtime.capabilities['wired_editor'] = False
        await self.refresh.tick()
        self.assertEqual(self.transport.sequence, 1)

    async def test_any_pending_receipt_and_operation_lock_block_reads(self):
        await self.settle()
        async with self.runtime.operation:
            await self.refresh.tick()
        self.store.begin('keypad_edit', 40003, {'stages': []})
        await self.refresh.tick()
        self.assertEqual(self.transport.sequence, 1)

    async def test_failed_reads_back_off_and_stop_after_three_until_new_session(self):
        await self.settle()
        with patch.object(self.keypad, 'read', new=AsyncMock(side_effect=TimeoutError)) as read:
            await self.refresh.tick()
            self.assertEqual(read.call_count, 1)
            self.now += 29999
            await self.refresh.tick()
            self.assertEqual(read.call_count, 1)
            self.now += 1
            await self.refresh.tick()
            self.now += 120000
            await self.refresh.tick()
            self.now += 1000000
            await self.refresh.tick()
            self.assertEqual(read.call_count, 3)
            self.assertEqual(self.refresh.progress['state'], 'exhausted')
            self.runtime.session()
            await self.settle()
            await self.refresh.tick()
            self.assertEqual(read.call_count, 4)
            self.assertTrue(all(call.args == (40001,) for call in read.call_args_list))
            self.assertEqual(read.call_args.kwargs['background_deadline'], self.now + 10000)

    async def test_foreground_waits_for_current_read_then_owns_priority(self):
        await self.settle()
        entered, release, foreground = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def read(*args, **kwargs):
            entered.set()
            await release.wait()
        async def user_request():
            async with self.runtime.foreground_request():
                foreground.set()
        with patch.object(self.keypad, 'read', side_effect=read):
            task = asyncio.create_task(self.refresh.tick())
            await entered.wait()
            user = asyncio.create_task(user_request())
            await asyncio.sleep(0)
            self.assertFalse(foreground.is_set())
            release.set()
            await asyncio.gather(task, user)
        self.assertTrue(foreground.is_set())
        self.assertIsNone(self.runtime._background_read_task)

    async def test_foreground_deferrals_do_not_exhaust_retry_budget(self):
        await self.settle()
        with patch.object(self.keypad, 'read', side_effect=BackgroundReadDeferred):
            for _ in range(5):
                await self.refresh.tick()
                self.assertEqual(self.refresh.progress['state'], 'waiting_foreground')
                self.assertEqual(self.refresh.schedule[40001][0], 0)
                self.now += 1000
        await self.refresh.tick()
        self.assertIn(40001, self.runtime.core.controllers.bindings)

    async def test_session_change_discards_old_attempt_schedule(self):
        await self.settle()
        async def crossed(*args, **kwargs):
            self.runtime.session()
            raise RuntimeError('Changed session')
        with patch.object(self.keypad, 'read', side_effect=crossed):
            await self.refresh.tick()
        self.assertEqual(self.refresh.schedule[40001][0], 0)
        await self.refresh.tick()
        self.assertEqual(self.refresh.schedule, {})

    async def test_cancellation_releases_background_reservation(self):
        await self.settle()
        with patch.object(self.keypad, 'read', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.refresh.tick()
        self.assertIsNone(self.runtime._background_read_task)
        self.assertTrue(self.runtime._background_read_idle.is_set())

    async def test_observation_worker_runs_shared_refresher_without_parallel_task(self):
        stop = asyncio.Event()
        async def tick():
            self.assertIs(asyncio.current_task(), self.runtime._observation_task)
            stop.set()
            raise asyncio.CancelledError
        with patch.object(self.refresh, 'tick', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime.observation_refresh(settle=0)
        self.assertTrue(stop.is_set())
        self.assertIsNone(self.runtime._observation_task)


if __name__ == '__main__':
    unittest.main()
