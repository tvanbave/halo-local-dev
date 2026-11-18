"""Synthetic-only bounded recovery; never restart an association."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import test_automatic_first_claim as auto_tests
import test_claim_development as base
from avionmesh_pi.association import open_envelope


class SafeClaimRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ClaimDevelopmentTests.asyncSetUp
    asyncTearDown = base.ClaimDevelopmentTests.asyncTearDown
    automatic = auto_tests.AutomaticFirstClaimTests.automatic
    rejected_initial = auto_tests.AutomaticFirstClaimTests.rejected_initial

    async def completed(self):
        await self.claims.discover()
        with patch.object(self.runtime, 'exchange', side_effect=asyncio.TimeoutError):
            with self.assertRaises(asyncio.TimeoutError):
                await self.claims.claim(base.IDENTITY, 'Synthetic', 93, inventory_complete=True)
        receipt, = self.store.journals()
        self.runtime.mesh_ready = self.transport.ready = True
        return receipt

    async def test_complete_checkpoint_only_pings_and_verifies(self):
        receipt = await self.completed()
        starts, scans = len(self.transport.starts), self.transport.scans
        result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'verified')
        self.assertEqual(result['ping_attempts'], 1)
        self.assertEqual((len(self.transport.starts), self.transport.scans), (starts, scans))
        self.assertEqual(self.store.journals()[0]['status'], 'verified')

    async def test_three_failed_proofs_keep_receipt(self):
        receipt = await self.completed()
        with patch.object(self.runtime, 'exchange', side_effect=asyncio.TimeoutError) as exchange:
            result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(exchange.call_count, 3)
        self.assertEqual(result['outcome'], 'proof_unavailable')
        self.assertEqual(self.store.journals()[0], receipt)
        self.assertEqual(len(self.transport.starts), 1)

    async def test_initial_checkpoint_retires_without_association(self):
        receipt = await self.rejected_initial()
        before = len(self.transport.frames)
        result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'pre_key_retired')
        self.assertEqual(result['ping_attempts'], 0)
        self.assertEqual([open_envelope(f)[0] for f in self.transport.frames[before:]], [0])
        self.assertFalse(self.store.inventory()['devices'])

    async def test_bounded_wait_can_reconnect(self):
        receipt = await self.completed()
        self.runtime.mesh_ready = self.transport.ready = False
        async def reconnect(_):
            self.runtime.mesh_ready = self.transport.ready = True
        with patch('avionmesh_pi.commissioning.asyncio.sleep', side_effect=reconnect):
            result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'verified')

    async def test_bounded_wait_expires_without_transmission(self):
        receipt = await self.completed()
        self.runtime.mesh_ready = self.transport.ready = False
        before = len(self.transport.frames)
        with patch('avionmesh_pi.commissioning.asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(sleep.call_count, 20)
        self.assertEqual(result['outcome'], 'transport_unavailable')
        self.assertEqual(len(self.transport.frames), before)

    async def test_cancellation_propagates_and_releases_guard(self):
        receipt = await self.completed()
        with patch.object(self.runtime, 'exchange', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.claims.recover_safe_pending(receipt['id'])
        self.assertFalse(self.claims._safe_recovery_active)
        self.assertEqual(self.store.journals()[0], receipt)

    async def test_changed_receipt_during_wait_stops(self):
        receipt = await self.completed()
        self.runtime.mesh_ready = self.transport.ready = False
        async def change(_):
            self.store.db.execute("UPDATE journals SET status='retired' WHERE id=?", (receipt['id'],))
            self.runtime.mesh_ready = self.transport.ready = True
        with patch('avionmesh_pi.commissioning.asyncio.sleep', side_effect=change):
            result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'receipt_changed')
        self.assertEqual(result['ping_attempts'], 0)

    async def test_competing_operation_and_unknown_checkpoint_fail_closed(self):
        receipt = await self.completed()
        async with self.runtime.operation:
            result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'competing_operation')
        detail = dict(receipt['detail'], association_complete=False, association_phase='network_key')
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), receipt['id']))
        result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'unsafe_checkpoint')
        self.assertEqual(result['ping_attempts'], 0)

    async def test_transport_error_is_not_retried(self):
        receipt = await self.completed()
        with patch.object(self.runtime, 'exchange', side_effect=RuntimeError('lost session')) as exchange:
            result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(exchange.call_count, 1)
        self.assertEqual(result['outcome'], 'recovery_blocked')
