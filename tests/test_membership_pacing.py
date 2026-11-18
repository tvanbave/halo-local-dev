"""Synthetic receiver processing deadlines; no radio or real sleeping required."""
import asyncio
import unittest
from unittest.mock import patch

from avionmesh_pi.protocol import decrypt_mcp
import test_membership_verified as verified


class PacedGroupTransport(verified.GroupTransport):
    """A receiver that cannot process another command for 150 ms.

    Replies and writes establish the deadline independently of the implementation's
    sleeps. This catches missing read/read, read/write and write/read spacing.
    """
    def __init__(self):
        super().__init__()
        self.now = 0.0
        self.ready_at = 0.0
        self.sleep_delays = []
        self.too_early = []
        self.drop_postwrite_read = False
        self.dropped_reads = 0
        self.on_sleep = None

    async def sleep(self, seconds):
        self.sleep_delays.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()

    async def send(self, frame):
        packet = decrypt_mcp(frame, verified.KEY)
        if packet.payload[1] != 3:
            return await super().send(frame)
        if self.now + 1e-9 < self.ready_at:
            self.too_early.append(packet.payload[0])
            raise asyncio.TimeoutError('Synthetic receiver still processing prior command')
        if packet.payload[0] in (1, 4) and self.mutations and self.drop_postwrite_read:
            # Submission succeeds but the device never replies. The transaction
            # must remain uncertain; neither the read nor setter is auto-retried.
            self.frames.append(frame)
            self.dropped_reads += 1
            return
        await super().send(frame)
        if packet.payload[0] in (2, 5):
            self.ready_at = self.now + 0.150

    def reply(self, target, payload, **kwargs):
        super().reply(target, payload, **kwargs)
        self.ready_at = self.now + 0.150


class MembershipPacingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await verified.VerifiedMembershipTests.asyncSetUp(self)
        self.transport = PacedGroupTransport()
        self.transport.receiver = self.runtime.observe
        self.runtime.transport = self.transport
        self.sleep_patch = patch('avionmesh_pi.membership.asyncio.sleep', self.transport.sleep)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    asyncTearDown = verified.VerifiedMembershipTests.asyncTearDown

    async def test_add_and_remove_wait_for_receiver_and_preserve_other_membership(self):
        self.transport.tables[40003] = [293, 0, 0, 0]
        added = await self.member.change(40003, 291, True)
        self.assertTrue(added['membership_verified'])
        self.assertTrue(added['desired_matched'])
        self.assertEqual(set(self.transport.tables[40003]) - {0}, {291, 293})
        removed = await self.member.change(40003, 291, False)
        self.assertTrue(removed['membership_verified'])
        self.assertTrue(removed['desired_matched'])
        self.assertEqual(self.transport.tables[40003], [293, 0, 0, 0])
        self.assertEqual(self.transport.mutations, [(40003, 291, True), (40003, 291, False)])
        self.assertEqual(self.transport.too_early, [])
        self.assertTrue(self.transport.sleep_delays)
        self.assertTrue(all(delay >= 0.150 for delay in self.transport.sleep_delays))
        self.assertEqual(self.store.journals(pending=True), [])

    async def test_missing_postwrite_reply_retains_receipt_without_setter_retry(self):
        self.transport.drop_postwrite_read = True
        original = self.runtime.exchange

        async def short_timeout(plaintext, predicate, timeout=3, *, generation=None):
            return await original(plaintext, predicate, timeout=0.01, generation=generation)

        with patch.object(self.runtime, 'exchange', short_timeout):
            with self.assertRaises(asyncio.TimeoutError):
                await self.member.change(40003, 291, True)
        self.assertEqual(self.transport.too_early, [])
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])
        self.assertEqual(self.transport.dropped_reads, 1)
        pending = self.store.journals(pending=True)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'uncertain')
        self.assertEqual(pending[0]['detail']['baseline_table']['groups'], [])
        self.assertEqual(pending[0]['detail']['verification_failure'], {
            'phase': 'postwrite_readback', 'reason': 'timeout',
            'session_changed': False, 'mesh_ready': True})
        self.assertNotIn(40003, next(g for g in self.store.inventory()['groups'] if g['group_id'] == 291)['members'])

    async def test_session_change_during_baseline_wait_prevents_setter(self):
        def change_session():
            if self.transport.group_counts == 2:
                self.runtime.session()
        self.transport.on_sleep = change_session
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.transport.mutations, [])
        self.assertEqual(self.transport.too_early, [])
        self.assertEqual(self.store.journals(), [])

    async def test_session_change_during_postwrite_wait_prevents_read_dispatch(self):
        def change_session():
            if self.transport.mutations:
                self.runtime.session()
        self.transport.on_sleep = change_session
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])
        self.assertEqual(self.transport.too_early, [])
        # Empty baseline needs exactly two COUNTs; no postwrite read may be sent
        # with the generation captured before the session changed during waiting.
        self.assertEqual(self.transport.group_counts, 2)
        receipt = self.store.journals(pending=True)[0]
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertEqual(receipt['detail']['verification_failure'], {
            'phase': 'postwrite_readback', 'reason': 'runtime_error',
            'session_changed': True, 'mesh_ready': True})

    async def test_cancellation_during_postwrite_wait_keeps_uncertain_receipt(self):
        def cancel_wait():
            if self.transport.mutations:
                raise asyncio.CancelledError('Synthetic cancellation during device settle')
        self.transport.on_sleep = cancel_wait
        with self.assertRaises(asyncio.CancelledError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])
        self.assertEqual(self.transport.group_counts, 2)
        self.assertEqual(self.transport.too_early, [])
        receipt = self.store.journals(pending=True)[0]
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertEqual(receipt['detail']['verification_failure'], {
            'phase': 'postwrite_settle', 'reason': 'cancelled',
            'session_changed': False, 'mesh_ready': True})
        self.assertEqual(receipt['detail']['baseline_table']['groups'], [])
        self.assertNotIn(40003, next(g for g in self.store.inventory()['groups'] if g['group_id'] == 291)['members'])
