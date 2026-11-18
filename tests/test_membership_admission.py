"""Concurrent group admission using synthetic transports and durable receipts."""
import asyncio
import unittest
from unittest.mock import patch

import test_membership_verified as verified


class MembershipAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = verified.VerifiedMembershipTests.asyncSetUp
    asyncTearDown = verified.VerifiedMembershipTests.asyncTearDown

    async def waiting_request(self):
        await self.runtime.operation.acquire()
        task = asyncio.create_task(self.member.change_many([40003], 291, True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return task

    async def test_batch_waits_for_current_read_and_writes_once(self):
        task = await self.waiting_request()
        try:
            self.assertFalse(task.done())
            self.assertEqual(self.transport.frames, [])
        finally:
            self.runtime.operation.release()
        result = await task
        self.assertTrue(result['complete'])
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])
        self.assertEqual(self.store.journals(pending=True), [])

    async def test_single_change_waits_for_current_read(self):
        await self.runtime.operation.acquire()
        task = asyncio.create_task(self.member.change(40003, 291, True))
        await asyncio.sleep(0)
        self.runtime.operation.release()
        result = await task
        self.assertTrue(result['membership_verified'])
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])

    async def test_pending_receipt_created_while_waiting_blocks_dispatch(self):
        task = await self.waiting_request()
        receipt = self.store.begin('claim', 40001, {'uuid': '00' * 16})
        self.runtime.operation.release()
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(pending=True)[0]['id'], receipt)
        self.assertFalse(self.runtime.operation.locked())

    async def test_session_change_while_waiting_blocks_dispatch(self):
        task = await self.waiting_request()
        self.runtime.session()
        self.runtime.operation.release()
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertFalse(self.runtime.operation.locked())

    async def test_inventory_change_while_waiting_requires_new_selection(self):
        task = await self.waiting_request()
        inventory = self.store.inventory()
        inventory['groups'][0]['name'] = 'Changed'
        self.store.set('inventory', inventory)
        self.runtime.operation.release()
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(self.transport.frames, [])

    async def test_cancelled_waiter_never_dispatches_or_releases_owner_lock(self):
        task = await self.waiting_request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.runtime.operation.locked())
        self.runtime.operation.release()
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def test_wait_timeout_is_bounded_and_does_not_dispatch(self):
        with patch('avionmesh_pi.membership._ADMISSION_WAIT_SECONDS', 0.01):
            await self.runtime.operation.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    await self.member.change_many([40003], 291, True)
                self.assertTrue(self.runtime.operation.locked())
            finally:
                self.runtime.operation.release()
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])
