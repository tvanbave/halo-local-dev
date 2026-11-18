"""Actual control-lock admission deadlines; no live transport or broker."""
import asyncio
import tempfile
import unittest

from avionmesh_pi.errors import OperationNotStarted
from avionmesh_pi.protocol import decrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import FakeTransport, INVENTORY, KEY


class ControlAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.runtime.mesh_ready = True
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('Expected lock boundary was not reached')

    def queued(self):
        return len(self.runtime.operation._waiters or ())

    async def command(self, timeout=.02):
        return await self.runtime.control(40002, {'brightness': 128}, confirm=False,
                                          admission_timeout=timeout)

    async def manual_owner(self, entered, release):
        async with self.runtime.operation:
            entered.set()
            await release.wait()

    async def test_actual_acquisition_holds_lock_through_slow_dispatch_before_successor(self):
        await self.runtime.operation.acquire()
        setter_entered, setter_release = asyncio.Event(), asyncio.Event()
        manual_entered, manual_release = asyncio.Event(), asyncio.Event()
        send = self.transport.send
        async def slow(frame):
            if decrypt_mcp(frame, KEY).payload[0] == 0:
                setter_entered.set()
                await setter_release.wait()
            await send(frame)
        self.transport.send = slow
        command = self.task(self.command())
        await self.until(lambda: self.queued() == 1)
        manual = self.task(self.manual_owner(manual_entered, manual_release))
        await self.until(lambda: self.queued() == 2)
        self.runtime.operation.release()
        await self.until(setter_entered.is_set)
        await asyncio.sleep(.04)  # admission deadline must not cancel a setter
        self.assertFalse(command.done())
        self.assertFalse(manual_entered.is_set())
        setter_release.set()
        self.assertTrue((await command)['sent'])
        await self.until(manual_entered.is_set)
        self.assertTrue(self.runtime.operation.locked())
        manual_release.set()
        await manual
        self.assertEqual(len(self.transport.frames), 2)
        self.assertFalse(self.runtime.operation.locked())

    async def test_timeout_behind_manual_successor_never_releases_its_lock(self):
        await self.runtime.operation.acquire()
        entered, release = asyncio.Event(), asyncio.Event()
        manual = self.task(self.manual_owner(entered, release))
        await self.until(lambda: self.queued() == 1)
        command = self.task(self.command())
        await self.until(lambda: self.queued() == 2)
        self.runtime.operation.release()
        await self.until(entered.is_set)
        with self.assertRaises(OperationNotStarted) as error:
            await command
        self.assertEqual(error.exception.code, 'mesh_busy')
        self.assertTrue(self.runtime.operation.locked())
        self.assertEqual(self.queued(), 0)
        self.assertEqual(self.transport.frames, [])
        release.set()
        await manual
        self.assertFalse(self.runtime.operation.locked())

    async def test_cancel_waiter_leaves_existing_owner_untouched(self):
        await self.runtime.operation.acquire()
        command = self.task(self.command(timeout=1))
        await self.until(lambda: self.queued() == 1)
        command.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await command
        self.assertTrue(self.runtime.operation.locked())
        self.assertEqual(self.queued(), 0)
        self.assertEqual(self.transport.frames, [])
        self.runtime.operation.release()

    async def test_cancel_at_acquire_completion_cleans_once_before_manual_successor(self):
        owner = [None]
        class CancellingLock(asyncio.Lock):
            async def acquire(lock):
                result = await super().acquire()
                if owner[0] is not None:
                    asyncio.get_running_loop().call_soon(owner[0].cancel)
                    owner[0] = None
                return result
        self.runtime.operation = CancellingLock()
        await self.runtime.operation.acquire()
        command = self.task(self.command(timeout=1))
        owner[0] = command
        await self.until(lambda: self.queued() == 1)
        entered, release = asyncio.Event(), asyncio.Event()
        manual = self.task(self.manual_owner(entered, release))
        await self.until(lambda: self.queued() == 2)
        self.runtime.operation.release()
        with self.assertRaises(asyncio.CancelledError):
            await command
        await self.until(entered.is_set)
        await asyncio.sleep(0)  # allow both direct and done-callback cleanup
        self.assertTrue(self.runtime.operation.locked())
        self.assertEqual(self.transport.frames, [])
        release.set()
        await manual
        self.assertFalse(self.runtime.operation.locked())

    async def test_inventory_change_while_waiting_refuses_before_setter(self):
        await self.runtime.operation.acquire()
        command = self.task(self.command(timeout=1))
        await self.until(lambda: self.queued() == 1)
        data = self.store.inventory()
        data['devices'][1]['exposed'] = False
        self.store.import_inventory(data)
        self.runtime.operation.release()
        with self.assertRaises(OperationNotStarted) as error:
            await command
        self.assertEqual(error.exception.code, 'inventory_changed')
        self.assertEqual(self.transport.frames, [])
        self.assertFalse(self.runtime.operation.locked())

    async def test_new_pending_operation_while_waiting_refuses_before_setter(self):
        await self.runtime.operation.acquire()
        command = self.task(self.command(timeout=1))
        await self.until(lambda: self.queued() == 1)
        self.store.begin('remove', 40002, {})
        self.runtime.operation.release()
        with self.assertRaisesRegex(RuntimeError, 'quarantined'):
            await command
        self.assertEqual(self.transport.frames, [])
        self.assertFalse(self.runtime.operation.locked())

    async def test_session_change_while_waiting_refuses_before_setter(self):
        await self.runtime.operation.acquire()
        command = self.task(self.command(timeout=1))
        await self.until(lambda: self.queued() == 1)
        self.runtime.session()
        self.runtime.operation.release()
        with self.assertRaises(OperationNotStarted) as error:
            await command
        self.assertEqual(error.exception.code, 'session_changed')
        self.assertEqual(self.transport.frames, [])
        self.assertFalse(self.runtime.operation.locked())

    async def test_default_http_style_control_still_refuses_busy_immediately(self):
        await self.runtime.operation.acquire()
        with self.assertRaisesRegex(RuntimeError, 'Another mesh operation is active'):
            await self.runtime.control(40002, {'brightness': 128}, confirm=False)
        self.assertEqual(self.queued(), 0)
        self.assertTrue(self.runtime.operation.locked())
        self.runtime.operation.release()

