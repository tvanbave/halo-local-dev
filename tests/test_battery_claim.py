"""Synthetic saved model126 claim proof; authenticated wake is never proof alone."""
import asyncio
import copy
import unittest
from unittest.mock import patch

import test_anyplace_claim as base

REAL_SLEEP = asyncio.sleep


class BatteryClaimTests(unittest.IsolatedAsyncioTestCase):
    receipt = base.AnyplaceClaimTests.receipt
    mutate = base.AnyplaceClaimTests.mutate
    start = base.AnyplaceClaimTests.start
    assert_clean = base.AnyplaceClaimTests.assert_clean

    async def asyncSetUp(self):
        await base.AnyplaceClaimTests.asyncSetUp(self)
        self.on_settle = None
        async def settle(seconds):
            if seconds:
                if self.on_settle:
                    await self.on_settle()
                self.now += round(seconds * 1000)
            await REAL_SLEEP(0)
        self.sleep_patch = patch('avionmesh_pi.commissioning.asyncio.sleep', side_effect=settle)
        self.sleep_patch.start()

    async def asyncTearDown(self):
        self.sleep_patch.stop()
        await base.AnyplaceClaimTests.asyncTearDown(self)

    def checkpoint(self):
        identifier = base.AnyplaceClaimTests.checkpoint(self)
        self.mutate(identifier, product_id=126, name='Synthetic Battery Keypad')
        return identifier

    # Exercise the real authenticated Runtime.observe path, not injected waiter results.
    test_nonce_proof_not_wake_alone = base.AnyplaceClaimTests.test_reconcile_requires_nonce_proof_not_wake_alone
    test_ack_echo_source_nonce_sequence = base.AnyplaceClaimTests.test_ack_echo_wrong_source_nonce_and_old_sequence_are_not_proof
    test_missing_checkpoint = base.AnyplaceClaimTests.test_missing_complete_checkpoint_never_waits_or_transmits
    test_wrong_hash = base.AnyplaceClaimTests.test_wrong_hash_checkpoint_never_waits_or_transmits
    test_receipt_change_at_allocation = base.AnyplaceClaimTests.test_receipt_mutation_during_allocation_stops_before_ping
    test_receipt_change_before_reply = base.AnyplaceClaimTests.test_receipt_mutation_before_reply_cannot_accept_claim
    test_session_change_while_waiting = base.AnyplaceClaimTests.test_session_change_while_waiting_aborts_immediately
    test_wrong_old_wake = base.AnyplaceClaimTests.test_old_received_wrong_source_and_shape_wakes_are_ignored
    test_automatic_recovery_needs_owner = base.AnyplaceClaimTests.test_automatic_recovery_requires_explicit_owner_wake

    async def test_pending_source_settles_then_exactly_one_ping_accepts_without_model_claim(self):
        identifier = self.checkpoint()
        self.assertNotIn(40001, self.runtime.core.devices)
        times = []
        self.transport.on_ping = lambda: times.append(self.now)
        task = await self.start(identifier)
        self.assertEqual(self.claims.progress[40001]['phase'], 'waiting_for_wake')
        self.assertEqual(self.transport.frames, [])
        self.transport.wake()
        result = await task
        self.assertEqual(times, [250])
        self.assertTrue(result['verified'])
        self.assertFalse(result['model_verified'])
        self.assertFalse(result['association_retried'])
        self.assertFalse(result['setup_automatically_resumed'])
        self.assertEqual(len(self.transport.pings), 1)
        self.assertEqual(self.transport.associations, [])
        self.assertEqual(self.claims.progress[40001]['phase'], 'verified')
        self.assert_clean()

    async def test_no_wake_sends_nothing_preserves_exact_receipt(self):
        identifier = self.checkpoint()
        original = copy.deepcopy(self.receipt(identifier))
        with patch('avionmesh_pi.commissioning.BATTERY_CLAIM_WAKE_WAIT', .01):
            result = await self.claims.reconcile(identifier)
        self.assertFalse(result['verified'])
        self.assertEqual(self.receipt(identifier), original)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.claims.progress[40001]['phase'], 'needs_review')
        self.assert_clean()

    async def test_cancelled_wake_consumed_even_after_duplicate_cache_expires(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        frame = self.transport.wake()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.now += 6000
        with patch('avionmesh_pi.commissioning.BATTERY_CLAIM_WAKE_WAIT', .01):
            task = await self.start(identifier)
            self.runtime.observe(frame, self.now)
            self.assertFalse((await task)['verified'])
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_cancel_during_settle_cleans_up_without_ping(self):
        identifier = self.checkpoint()
        reached = asyncio.Event()
        async def hold():
            reached.set()
            await asyncio.Future()
        self.on_settle = hold
        task = await self.start(identifier)
        self.transport.wake()
        await reached.wait()
        self.assertEqual(self.claims.progress[40001]['phase'], 'settling')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.receipt(identifier)['status'], 'uncertain')
        self.assert_clean()

    async def test_session_receipt_retirement_and_competitor_changes_during_settle(self):
        for change in ('session', 'receipt', 'retired', 'competitor', 'inventory'):
            with self.subTest(change=change):
                identifier = self.checkpoint()
                async def mutate():
                    if change == 'session': self.runtime.session()
                    elif change == 'receipt': self.mutate(identifier, name='Changed')
                    elif change == 'retired': self.store.set('retired_claims', [identifier])
                    elif change == 'competitor': self.store.begin('claim', 40002, {})
                    else:
                        data = self.store.inventory()
                        data['devices'].append({'device_id':40001,'product_id':126,'name':'Other','exposed':False})
                        self.store.set('inventory',data)
                self.on_settle = mutate
                task = await self.start(identifier)
                self.transport.wake()
                with self.assertRaises((RuntimeError, ValueError)):
                    await task
                self.assertEqual(self.transport.frames, [])
                self.assert_clean()
                # Fresh isolated receipt/inventory for the next subcase.
                self.store.db.execute('DELETE FROM journals')
                self.store.set('retired_claims', [])
                data = self.store.inventory(); data['devices'] = []; self.store.set('inventory', data)
                self.runtime.mesh_ready = True

    async def test_dispatch_1500ms_boundary_and_late_first_wake_not_renewed(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        self.transport.wake()
        self.now += 1500
        self.assertTrue((await task)['verified'])
        self.assertEqual(len(self.transport.pings), 1)
        self.assert_clean()

    async def test_late_dispatch_and_allocation_delays_send_nothing(self):
        for delay in ('resume', 'token', 'sequence'):
            with self.subTest(delay=delay):
                identifier = self.checkpoint()
                allocate = self.store.allocate_sequence
                from avionmesh_pi.identity import ping_token
                def late_token(store):
                    token = ping_token(store); self.now += 1501; return token
                def late_sequence():
                    seq = allocate(); self.now += 1501; return seq
                with patch('avionmesh_pi.commissioning.ping_token', side_effect=late_token if delay=='token' else ping_token):
                    if delay == 'sequence': self.store.allocate_sequence = late_sequence
                    task = await self.start(identifier)
                    self.transport.wake()
                    if delay == 'resume':
                        self.now += 1500
                        self.transport.wake()  # Does not replace original receipt timestamp.
                        self.now += 1
                    with self.assertRaisesRegex(RuntimeError, 'wake dispatch'):
                        await task
                self.store.allocate_sequence = allocate
                self.assertEqual(self.transport.frames, [])
                self.assert_clean()
                self.store.db.execute('DELETE FROM journals')

    async def test_unauthenticated_and_overage_wakes_never_open_window(self):
        from avionmesh_pi.protocol import encrypt_mcp
        from test_association import KEY
        identifier = self.checkpoint()
        task = await self.start(identifier)
        frame = encrypt_mcp(b"\0\x80\x73\x0b\x1c" + bytes(5), KEY, 300, 40001)
        damaged = bytearray(frame)
        damaged[8] ^= 1
        self.runtime.observe(bytes(damaged), self.now)
        self.now += 1001
        self.runtime.observe(frame, self.now - 1001)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(self.transport.frames, [])
        self.transport.wake()
        self.assertTrue((await task)['verified'])
        self.assertEqual(len(self.transport.pings), 1)
        self.assert_clean()
