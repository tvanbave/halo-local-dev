"""Automatic removal checks use fresh reads only, including delayed/reset failures."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from avionmesh_pi.lifecycle import Lifecycle
from avionmesh_pi.association import uuid_hash
from test_lifecycle import LifecycleTests, UUID


class RemovalVerificationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = LifecycleTests.asyncSetUp
    asyncTearDown = LifecycleTests.asyncTearDown

    def match(self):
        return [{'uuid': UUID, 'uuid_hash': uuid_hash(bytes.fromhex(UUID))}]

    async def test_delayed_unpaired_reply_completes_without_second_removal(self):
        self.scan.discover = AsyncMock(side_effect=[[], [], self.match()])
        with patch('avionmesh_pi.lifecycle.asyncio.sleep', new=AsyncMock()) as delay:
            result = await self.lifecycle.remove_and_verify(40002)
        self.assertTrue(result['removed'])
        self.assertEqual(result['verification_attempts'], 3)
        self.assertEqual(result['verification'], 'verified')
        self.assertFalse(result['needs_exact_uuid_scan'])
        self.assertFalse(result['write_retried'])
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(delay.await_count, 2)
        self.assertEqual(self.store.journals()[-1]['status'], 'verified')
        self.assertNotIn(40002, self.runtime.core.devices)

    async def test_missing_unpaired_reply_is_bounded_and_keeps_receipt(self):
        with patch('avionmesh_pi.lifecycle.asyncio.sleep', new=AsyncMock()):
            result = await self.lifecycle.remove_and_verify(40002)
        self.assertFalse(result['removed'])
        self.assertTrue(result['requires_review'])
        self.assertEqual(result['verification_attempts'], 3)
        self.assertEqual(self.scan.calls, 3)
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        with self.assertRaises(RuntimeError):
            await self.lifecycle.remove_and_verify(40002)
        self.assertEqual(len(self.transport.frames), 1)
        self.assertIn(40002, self.runtime.core.devices)

    async def test_scan_error_keeps_removal_receipt_for_manual_verification(self):
        self.scan.discover = AsyncMock(side_effect=OSError('disconnected'))
        result = await self.lifecycle.remove_and_verify(40002)
        self.assertEqual(result['verification_attempts'], 1)
        self.assertTrue(result['requires_review'])
        self.assertEqual(result['transaction'], self.store.journals()[-1]['id'])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)

    async def test_scan_timeout_is_bounded_and_retained(self):
        async def timeout(awaitable, timeout):
            self.assertEqual(timeout, 7)
            awaitable.close()
            raise asyncio.TimeoutError
        with patch('avionmesh_pi.lifecycle.asyncio.wait_for', side_effect=timeout):
            result = await self.lifecycle.remove_and_verify(40002)
        self.assertTrue(result['requires_review'])
        self.assertEqual(result['verification_attempts'], 1)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')

    async def test_cancellation_propagates_and_restart_keeps_uncertain_receipt(self):
        self.scan.discover = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.lifecycle.remove_and_verify(40002)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)
        from support import synthetic_store as Store
        from test_service import KEY
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertTrue(any(d['device_id'] == 40002 for d in self.store.inventory()['devices']))

    async def test_disconnect_between_scans_stops_automatic_checks(self):
        async def disconnect(_):
            self.runtime.session()
        with patch('avionmesh_pi.lifecycle.asyncio.sleep', side_effect=disconnect):
            result = await self.lifecycle.remove_and_verify(40002)
        self.assertTrue(result['requires_review'])
        self.assertEqual(self.scan.calls, 1)
        self.assertEqual(result['verification_attempts'], 1)
        self.assertEqual(len(self.transport.frames), 1)

    async def test_transport_failure_never_starts_verification(self):
        self.transport.fail = True
        with self.assertRaises(RuntimeError):
            await self.lifecycle.remove_and_verify(40002)
        self.assertEqual(self.scan.calls, 0)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')

    async def test_publication_failure_still_reports_verified_removal(self):
        self.scan.results = self.match()
        with patch('avionmesh_pi.lifecycle.reload_inventory', side_effect=RuntimeError('publication')):
            result = await self.lifecycle.remove_and_verify(40002)
        self.assertTrue(result['removed'])
        self.assertTrue(result['publication_pending'])
        self.assertTrue(result['requires_review'])
        self.assertFalse(result['needs_exact_uuid_scan'])
        self.assertEqual(result['verification_attempts'], 1)
        self.assertEqual(self.scan.calls, 1)
        self.assertEqual(len(self.transport.frames), 1)


# The harness above is reused, not its existing test methods.
del LifecycleTests
