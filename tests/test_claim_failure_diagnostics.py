"""Failure diagnostics must not replace claim errors or prevent safe cleanup.

All faults use synthetic SQLite state and a fake transport. No radio, network,
installation state, or production credentials are used.
"""
import asyncio
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_claim_development as base
from avionmesh_pi.association import open_envelope
from avionmesh_pi.commissioning import ClaimValidationError


class DiagnosticWriteFailure:
    """Delegate SQLite operations, rejecting only the optional diagnostic write."""

    def __init__(self, connection):
        self.connection = connection
        self.rejected = 0

    def execute(self, sql, parameters=()):
        if sql.startswith('UPDATE journals SET detail=') and parameters:
            detail = json.loads(parameters[0])
            if 'failure_code' in detail:
                self.rejected += 1
                raise sqlite3.OperationalError('Synthetic diagnostic persistence failure')
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class ClaimFailureDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ClaimDevelopmentTests.asyncSetUp
    asyncTearDown = base.ClaimDevelopmentTests.asyncTearDown

    async def prepare_failure(self, error):
        await self.claims.discover()
        self.source_error_raised = False

        def fail_third_submission(frame):
            payload = open_envelope(frame)
            if payload[0] == 4 and payload[5] == 1:
                self.source_error_raised = True
                raise error

        self.transport.on_send = fail_third_submission

    async def claim_raises_original(self, error):
        with self.assertRaises(type(error)) as caught:
            await self.claims.claim(base.IDENTITY, 'Synthetic diagnostics fault', 93,
                                    inventory_complete=True)
        # asyncio.wait_for may recreate CancelledError across its task boundary,
        # especially on Python 3.9. Preserve cancellation, not object identity.
        if not isinstance(error, asyncio.CancelledError):
            self.assertIs(caught.exception, error)
        self.assertTrue(self.source_error_raised)

    async def assert_safe_pending(self, expected_status):
        receipt, = self.store.journals()
        self.assertIn(receipt['status'], expected_status)
        self.assertEqual(self.store.journals(pending=True), [receipt])
        self.assertEqual(receipt['detail']['association_phase'], 'public_key')
        self.assertEqual(receipt['detail']['submissions_attempted'], 3)
        self.assertEqual(receipt['detail']['submissions_accepted'], 2)
        self.assertFalse(receipt['detail']['association_complete'])
        self.assertFalse(self.store.inventory()['devices'])
        self.assertFalse(self.store.get('retired_addresses', []))
        self.assertEqual(self.store.get('sequence'), 0)
        self.assertEqual(len(self.transport.starts), 1)
        self.assertEqual([open_envelope(f)[0] for f in self.transport.frames], [0, 2, 4, 4])
        self.assertEqual(self.transport.disconnects, 1)
        self.assertFalse(self.transport.ready)
        self.assertFalse(self.runtime.mesh_ready)
        self.assertIn(receipt['id'], self.runtime.claim_isolated)
        self.assertFalse(self.runtime.operation.locked())
        self.assertFalse(self.runtime.raw_observers)

        # Even a later ready transport cannot bypass the pending reservation.
        self.transport.on_send = None
        self.transport.ready = self.runtime.mesh_ready = True
        before = len(self.transport.frames)
        with self.assertRaises(ClaimValidationError):
            await self.claims.claim(base.IDENTITY, 'Forbidden retry', 93,
                                    inventory_complete=True)
        self.assertEqual(len(self.transport.frames), before)
        self.assertEqual(self.store.journals(), [receipt])
        return receipt

    async def test_diagnostic_update_failure_preserves_timeout_and_marks_uncertain(self):
        error = asyncio.TimeoutError('Synthetic original write timeout')
        await self.prepare_failure(error)
        database = DiagnosticWriteFailure(self.store.db)
        with patch.object(self.store, 'db', database):
            await self.claim_raises_original(error)
        self.assertEqual(database.rejected, 1)
        receipt = await self.assert_safe_pending({'uncertain'})
        self.assertNotIn('failure_code', receipt['detail'])

    async def test_diagnostic_update_failure_preserves_cancellation_and_marks_uncertain(self):
        error = asyncio.CancelledError('Synthetic original cancellation')
        await self.prepare_failure(error)
        database = DiagnosticWriteFailure(self.store.db)
        with patch.object(self.store, 'db', database):
            await self.claim_raises_original(error)
        self.assertEqual(database.rejected, 1)
        receipt = await self.assert_safe_pending({'uncertain'})
        self.assertNotIn('failure_code', receipt['detail'])

    async def handler_read_failure(self, error):
        await self.prepare_failure(error)
        original = self.store.journals
        rejected = []

        def fail_handler_read(*args, **kwargs):
            if self.source_error_raised:
                rejected.append(True)
                raise sqlite3.OperationalError('Synthetic handler receipt read failure')
            return original(*args, **kwargs)

        with patch.object(self.store, 'journals', side_effect=fail_handler_read):
            await self.claim_raises_original(error)
        self.assertTrue(rejected)
        await self.assert_safe_pending({'attempted', 'uncertain'})

    async def test_handler_receipt_read_failure_still_cleans_up_and_preserves_timeout(self):
        await self.handler_read_failure(asyncio.TimeoutError('Synthetic original timeout'))

    async def test_handler_receipt_read_failure_still_cleans_up_and_preserves_cancellation(self):
        await self.handler_read_failure(asyncio.CancelledError('Synthetic original cancellation'))

    async def uncertainty_transition_failure(self, error):
        await self.prepare_failure(error)
        original = self.store.transition
        rejected = []

        def fail_uncertain_transition(identifier, expected, status):
            if (expected, status) == ('attempted', 'uncertain'):
                rejected.append(identifier)
                raise sqlite3.OperationalError('Synthetic uncertainty marking failure')
            return original(identifier, expected, status)

        with patch.object(self.store, 'transition', side_effect=fail_uncertain_transition):
            await self.claim_raises_original(error)
        self.assertEqual(len(rejected), 1)
        receipt = await self.assert_safe_pending({'attempted'})
        self.assertEqual(rejected, [receipt['id']])
        self.assertIn('failure_code', receipt['detail'])

    async def test_uncertainty_transition_failure_keeps_attempted_reservation_and_timeout(self):
        await self.uncertainty_transition_failure(asyncio.TimeoutError('Synthetic original timeout'))

    async def test_uncertainty_transition_failure_keeps_attempted_reservation_and_cancellation(self):
        await self.uncertainty_transition_failure(asyncio.CancelledError('Synthetic original cancellation'))

    async def test_postcommit_publication_failure_does_not_rewrite_verified_receipt(self):
        await self.claims.discover()
        database = DiagnosticWriteFailure(self.store.db)
        with patch.object(self.store, 'db', database), patch.object(
                self.runtime, 'session', side_effect=RuntimeError('Synthetic publication failure')):
            result = await self.claims.claim(base.IDENTITY, 'Synthetic accepted light', 93,
                                            inventory_complete=True)
        self.assertTrue(result['associated'])
        self.assertTrue(result['requires_review'])
        self.assertTrue(result['publication_pending'])
        self.assertEqual(database.rejected, 0)
        receipt, = self.store.journals()
        self.assertEqual(receipt['status'], 'verified')
        self.assertTrue(receipt['detail']['association_complete'])
        self.assertNotIn('failure_code', receipt['detail'])
        self.assertEqual(len(self.store.inventory()['devices']), 1)
        self.assertEqual(len(self.transport.starts), 1)
        self.assertEqual(self.transport.disconnects, 1)
        self.assertFalse(self.runtime.mesh_ready)
        self.assertFalse(self.runtime.operation.locked())
        self.assertFalse(self.runtime.raw_observers)
