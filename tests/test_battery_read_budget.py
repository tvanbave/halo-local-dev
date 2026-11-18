"""Synthetic model-126 read windows; no radio or hardware qualification."""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_editor import EditorTransport
from test_service import KEY


class BatteryReadBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 40001, 'product_id': 126, 'name': 'Synthetic battery', 'exposed': False}],
            'groups': []})
        self.now = 10
        self.transport = EditorTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        self.wakes = []
        self.requests = []
        self.wake_delays = []
        self.failures = set()
        self.keypad = KeypadService(self.runtime, sleeper=self.sleep)
        # Temporary operation observers must leave the exact service-owned
        # callback set intact, including battery cache contradiction monitoring.
        self.permanent_packet_observers = tuple(self.runtime.packet_observers)
        send = self.transport.send

        async def transport_send(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] in (1, 4):
                slot = payload[5] if payload[0] == 1 else 0
                self.requests.append((len(self.wakes), slot))
                if (len(self.wakes), slot) in self.failures:
                    self.now += 750
                    raise asyncio.TimeoutError('Synthetic lost read')
            await send(frame)

        self.transport.send = transport_send

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def sleep(self, seconds):
        self.now += round(seconds * 1000)
        if self.keypad.progress.get(40001) == 'settling':
            return
        if self.wake_delays:
            self.now += self.wake_delays.pop(0)

        def wake():
            self.wakes.append(self.now)
            self.runtime.observe(encrypt_mcp(b'\0\x80\x73\x0b\x1c' + bytes(5),
                KEY, self.transport.sequence, 40001), self.now)
            self.transport.sequence += 1

        asyncio.get_running_loop().call_soon(wake)

    async def test_two_rounds_share_two_restarts_then_fail_without_fifth_wake(self):
        self.failures = {(1, 2), (3, 2), (4, 2)}
        with self.assertRaises(asyncio.TimeoutError):
            await self.keypad.read(40001)
        self.assertEqual(len(self.wakes), 4)
        self.assertEqual([slot for wake, slot in self.requests if wake == 2], list(range(5)))
        self.assertEqual([slot for wake, slot in self.requests if wake == 4], [0, 1, 2])
        self.assertNotIn(40001, self.store.bindings())
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.runtime.waiters, [])

    async def test_two_restarts_across_two_rounds_allow_four_complete_windows(self):
        self.failures = {(1, 2), (3, 3)}
        result = await self.keypad.read(40001)
        self.assertEqual(len(self.wakes), 4)
        self.assertEqual([slot for wake, slot in self.requests if wake == 2], list(range(5)))
        self.assertEqual([slot for wake, slot in self.requests if wake == 4], list(range(5)))
        self.assertTrue(all(b - a >= 20000 for a, b in zip(self.wakes, self.wakes[1:])))
        self.assertEqual(self.store.bindings()[40001], result)
        self.assertEqual(self.transport.writes, [])

    async def test_second_wake_cannot_extend_original_180_second_deadline(self):
        self.wake_delays = [80000, 80000]
        with self.assertRaisesRegex(asyncio.TimeoutError, 'session deadline'):
            await self.keypad.read(40001)
        self.assertEqual(self.now, 180010)
        self.assertEqual([slot for _, slot in self.requests], list(range(5)))
        self.assertNotIn(40001, self.store.bindings())
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_wait_timeouts_use_remaining_session_budget(self):
        timeouts = []
        wait_for = asyncio.wait_for

        async def record(awaitable, timeout):
            if timeout > 1:
                timeouts.append(timeout)
            return await wait_for(awaitable, timeout)

        with patch('avionmesh_pi.keypad.asyncio.wait_for', record):
            await self.keypad.read(40001)
        self.assertEqual(timeouts, [180, 160])

    async def test_sequence_persistence_cannot_dispatch_after_session_deadline(self):
        allocate = self.store.allocate_sequence

        def delayed_allocate():
            result = allocate()
            self.now += 101
            return result

        self.store.allocate_sequence = delayed_allocate
        with self.assertRaisesRegex(asyncio.TimeoutError, 'request deadline'):
            await self.keypad._read_entry(40001, 0, True, self.runtime.generation,
                battery_deadline=self.now + 100)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.runtime.waiters, [])

    async def test_sequence_persistence_cannot_dispatch_after_request_deadline(self):
        allocate = self.store.allocate_sequence

        def delayed_allocate():
            result = allocate()
            self.now += 750
            return result

        self.store.allocate_sequence = delayed_allocate
        with self.assertRaisesRegex(asyncio.TimeoutError, 'request deadline'):
            await self.keypad._read_entry(40001, 0, True, self.runtime.generation,
                battery_deadline=self.now + 180000)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.runtime.waiters, [])

    async def test_late_reply_does_not_complete_expired_request(self):
        send = self.transport.send
        exchange = self.runtime.exchange

        async def late(frame):
            self.now += 750
            await send(frame)

        async def quick(command, predicate, timeout=3, **options):
            return await exchange(command, predicate, timeout=min(timeout, .01), **options)

        self.transport.send = late
        self.runtime.exchange = quick
        with self.assertRaises(asyncio.TimeoutError):
            await self.keypad._read_entry(40001, 0, True, self.runtime.generation,
                battery_deadline=self.now + 180000)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.transport.writes, [])

    async def test_task_resumption_after_deadline_does_not_accept_early_reply(self):
        send = self.transport.send

        async def delayed_return(frame):
            await send(frame)
            self.now += 750

        self.transport.send = delayed_return
        with self.assertRaisesRegex(asyncio.TimeoutError, 'request deadline'):
            await self.keypad._read_entry(40001, 0, True, self.runtime.generation,
                battery_deadline=self.now + 180000)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.transport.writes, [])
