"""Synthetic RL4 public-key retirement, without reset or association retries.

Every integration receipt is produced by the real claim engine and independent
ECDH Peer. No installation identifiers, state, or radio transport are used.
"""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_claim_development as base
from test_association import Peer
from avionmesh_pi import identity
from avionmesh_pi.association import open_envelope, uuid_hash


FINGERPRINT = uuid_hash(bytes.fromhex(base.IDENTITY))
_MISSING = object()


def public_key_receipt():
    """A synthetic schema fixture; integration tests generate actual receipts."""
    return {
        'id': 'synthetic-public-key-attempt', 'kind': 'claim',
        'status': 'uncertain', 'target': 40001,
        'detail': {
            'uuid': base.IDENTITY, 'uuid_hash': FINGERPRINT,
            'product_id': 93, 'name': 'Synthetic RL4',
            'inventory_complete': True, 'association_complete': False,
            'association_phase': 'public_key',
            'submissions_attempted': 3, 'submissions_accepted': 3,
            'setup_requested': False,
            'direct_bootstrap': False, 'fresh_mesh_first_claim': False,
            'bootstrap_address': None,
        },
    }


def invalid_profiles():
    for field, values in {
        'kind': ['remove', None],
        'status': ['prepared', 'attempted', 'verified', 'retired', None],
        'target': [True, False, 32895, 32898, 65408, 40001.0, '40001', None],
    }.items():
        for value in values:
            yield ('journal', field, value)
    for field, values in {
        'uuid': [base.IDENTITY.upper(), ' ' + base.IDENTITY, '00' * 16,
                 base.WIRED, base.ACCESSORY, base.IDENTITY[:-1], None],
        'uuid_hash': [FINGERPRINT + 1, True, float(FINGERPRINT), str(FINGERPRINT), None],
        'product_id': [True, 93.0, '93', 127, 91, None],
        'name': ['', 'x' * 129, 1, None],
        'inventory_complete': [False, 1, 'true', None],
        'association_complete': [True, 0, 'false', None],
        'association_phase': ['association', 'prepared', 'confirm', 'random',
                              'device_id', 'network_key', 'complete', None],
        'submissions_attempted': [True, False, 0, 1, 2, 4, 8, 3.0, '3', None],
        'submissions_accepted': [True, False, 0, 1, 2, 4, 8, 3.0, '3', None],
        'setup_requested': [True, 0, 'false', None],
    }.items():
        for value in values:
            yield ('detail', field, value)
    for field in ('uuid', 'uuid_hash', 'product_id', 'name', 'inventory_complete',
                  'association_complete', 'association_phase', 'submissions_attempted',
                  'submissions_accepted', 'setup_requested'):
        yield ('detail', field, _MISSING)


class PublicKeyProfileTests(unittest.TestCase):
    def test_exact_pre_address_public_key_range_including_ordinary_claims(self):
        for count in range(2, 8):
            for bootstrap in (False, True):
                with self.subTest(count=count, bootstrap=bootstrap):
                    receipt = public_key_receipt()
                    receipt['detail'].update(
                        submissions_attempted=count, submissions_accepted=count,
                        fresh_mesh_first_claim=bootstrap)
                    self.assertTrue(identity.rl4_public_key_claim_profile(receipt))
        for target in (32896, 32897, 32899, 65407):
            receipt = public_key_receipt()
            receipt['target'] = target
            receipt['detail']['name'] = 'x' * 128
            self.assertTrue(identity.rl4_public_key_claim_profile(receipt))

    def test_malformed_or_incomplete_evidence_fails_closed(self):
        for location, field, value in invalid_profiles():
            with self.subTest(location=location, field=field, value=value):
                receipt = public_key_receipt()
                container = receipt if location == 'journal' else receipt['detail']
                if value is _MISSING:
                    container.pop(field)
                else:
                    container[field] = value
                self.assertFalse(identity.rl4_public_key_claim_profile(receipt))

    def test_no_reset_declaration_or_bootstrap_identity_is_needed(self):
        receipt = public_key_receipt()
        for field in ('direct_bootstrap', 'fresh_mesh_first_claim', 'bootstrap_address'):
            receipt['detail'].pop(field)
        self.assertTrue(identity.rl4_public_key_claim_profile(receipt))

    def test_equal_counters_outside_public_key_range_still_fail_closed(self):
        for count in (True, False, -1, 0, 1, 8, 12, 3.0, '3', None):
            with self.subTest(count=count):
                receipt = public_key_receipt()
                receipt['detail'].update(submissions_attempted=count,
                                         submissions_accepted=count)
                self.assertFalse(identity.rl4_public_key_claim_profile(receipt))

    def test_catalog_mismatch_is_rejected_even_with_consistent_uuid_hash(self):
        for value in (base.WIRED, base.ACCESSORY, 'ff' * 16):
            receipt = public_key_receipt()
            receipt['detail'].update(uuid=value, uuid_hash=uuid_hash(bytes.fromhex(value)))
            self.assertFalse(identity.rl4_public_key_claim_profile(receipt))

    def test_missing_or_nonobject_receipt_evidence_is_rejected(self):
        for value in (None, [], 'claim', {}, {'detail': None}, {'detail': []}):
            with self.subTest(value=value):
                self.assertFalse(identity.rl4_public_key_claim_profile(value))


class PublicKeyRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.ClaimDevelopmentTests.asyncSetUp(self)
        self.runtime.capabilities['ordinary_claim'] = True

    asyncTearDown = base.ClaimDevelopmentTests.asyncTearDown

    def ready_again(self):
        self.runtime.mesh_ready = self.transport.ready = True

    async def accepted_inventory(self):
        await self.claims.discover()
        accepted = await self.claims.claim(base.SECOND, 'Already accepted', 93,
                                           inventory_complete=True)
        inventory = self.store.inventory()
        inventory['groups'] = [
            {'group_id': 601, 'name': 'Synthetic group',
             'members': [accepted['device_id']], 'exposed': False, 'protected': False},
            {'group_id': 602, 'name': 'Synthetic reservation',
             'members': [], 'exposed': False, 'protected': True},
        ]
        self.store.import_inventory(inventory)
        return copy.deepcopy(inventory)

    async def failed_public_key(self, *, bootstrap=False):
        if bootstrap:
            self.runtime.bootstrap = SimpleNamespace(
                automatic=True, selected_address='05:04:03:02:01:00',
                needs_first_claim=True)
        self.transport.peers[FINGERPRINT] = Peer()
        await self.claims.discover()
        send = self.transport.send
        wait_for = asyncio.wait_for

        async def no_second_chunk_reply(frame):
            payload = open_envelope(frame)
            if payload[0] == 4 and payload[5] == 1:
                # GATT accepted the submission, but its reply was lost. This is
                # deliberately different from a rejected send (3 attempted/2 accepted).
                self.transport.frames.append(frame)
                self.transport.peers[FINGERPRINT].respond(frame)
                return
            await send(frame)

        async def bounded_offline_wait(awaitable, timeout):
            return await wait_for(awaitable, min(timeout, 0.05 if timeout == 5 else 1))

        with patch.object(self.transport, 'send', side_effect=no_second_chunk_reply):
            with patch('avionmesh_pi.commissioning.asyncio.wait_for', side_effect=bounded_offline_wait):
                with self.assertRaises(asyncio.TimeoutError):
                    await self.claims.claim(base.IDENTITY, 'Interrupted RL4', 93,
                                            inventory_complete=True)
        receipt, = self.store.journals(pending=True)
        self.assertEqual(receipt['detail']['association_phase'], 'public_key')
        self.assertEqual(receipt['detail']['submissions_attempted'], 3)
        self.assertEqual(receipt['detail']['submissions_accepted'], 3)
        self.assertFalse(receipt['detail']['association_complete'])
        self.assertIn(receipt['id'], self.runtime.claim_isolated)
        self.assertIsNone(self.transport.peers[FINGERPRINT].target)
        self.assertFalse(self.transport.peers[FINGERPRINT].netkey)
        self.ready_again()
        return receipt

    def replace_receipt(self, receipt):
        self.store.db.execute('UPDATE journals SET kind=?,target=?,status=?,detail=? WHERE id=?',
                              (receipt['kind'], receipt['target'], receipt['status'],
                               json.dumps(receipt['detail']), receipt['id']))

    def assert_not_retired(self, receipt):
        current = next(j for j in self.store.journals() if j['id'] == receipt['id'])
        self.assertEqual(current['status'], 'uncertain')
        self.assertNotIn(receipt['id'], self.store.get('retired_claims', []))
        self.assertFalse(any(j['kind'] == 'claim_pre_key' for j in self.store.journals()))

    def assert_only_discovery(self, before):
        # open_envelope rejects encrypted MCP writes, so this also forbids Ping,
        # resets, controller setup, and all mesh commands during retirement.
        self.assertEqual([open_envelope(frame)[0] for frame in self.transport.frames[before:]], [0])

    async def assert_rejected(self, receipt, *, radio=False):
        before = len(self.transport.frames)
        with self.assertRaises((ValueError, RuntimeError)):
            await self.claims.reconcile_pre_key(receipt['id'])
        if radio:
            self.assert_only_discovery(before)
        else:
            self.assertEqual(len(self.transport.frames), before)
        self.assert_not_retired(receipt)

    async def test_real_three_of_three_checkpoint_retires_with_accepted_inventory(self):
        inventory = await self.accepted_inventory()
        receipt = await self.failed_public_key()
        before = len(self.transport.frames)
        sequence = self.store.get('sequence')
        starts = list(self.transport.starts)
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertTrue(result['pre_key_retired'])
        self.assertTrue(result['retired'])
        self.assertFalse(result['reset_verified'])
        self.assertFalse(result['association_retried'])
        self.assertFalse(result['automatic_reclaim'])
        self.assert_only_discovery(before)
        self.assertEqual(self.transport.starts, starts)
        self.assertEqual(self.store.inventory(), inventory)
        self.assertEqual(self.store.get('sequence'), sequence)
        self.assertIn(receipt['target'], self.store.get('retired_addresses'))
        self.assertIn(receipt['id'], self.store.get('retired_claims'))
        retained = next(j for j in self.store.journals() if j['id'] == receipt['id'])
        self.assertEqual(retained, dict(receipt, status='retired'))
        proof, = [j for j in self.store.journals() if j['kind'] == 'claim_pre_key']
        self.assertEqual(proof['detail']['original_receipt'], receipt)
        self.assertFalse(proof['detail']['owner_reset_declared'])
        self.assertFalse(proof['detail']['reset_dispatched'])
        self.assertFalse(proof['detail']['association_retried'])
        self.assertFalse(self.store.journals(pending=True))

    async def test_bootstrap_public_key_checkpoint_uses_same_pre_key_invariant(self):
        receipt = await self.failed_public_key(bootstrap=True)
        before = len(self.transport.frames)
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertTrue(result['pre_key_retired'])
        self.assertFalse(result['reset_verified'])
        self.assert_only_discovery(before)
        self.assertFalse(self.store.inventory()['devices'])

    async def test_bounded_safe_recovery_retires_and_never_repairs_automatically(self):
        inventory = await self.accepted_inventory()
        receipt = await self.failed_public_key()
        before = len(self.transport.frames)
        result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'pre_key_retired')
        self.assertEqual(result['ping_attempts'], 0)
        for field in ('association_retried', 'reset_dispatched', 'automatic_reclaim'):
            self.assertFalse(result[field])
        self.assert_only_discovery(before)
        self.assertEqual(self.store.inventory(), inventory)
        self.assertFalse(self.claims._safe_recovery_active)

    async def test_later_explicit_claim_burns_old_address_and_has_no_controller_setup(self):
        inventory = await self.accepted_inventory()
        receipt = await self.failed_public_key()
        starts = len(self.transport.starts)
        sequence = self.store.get('sequence')
        await self.claims.reconcile_pre_key(receipt['id'])
        self.assertEqual(len(self.transport.starts), starts)
        self.assertEqual(self.store.get('sequence'), sequence)
        self.transport.peers[FINGERPRINT] = Peer()
        result = await self.claims.claim(base.IDENTITY, 'Explicit retry', 93,
                                        inventory_complete=True, refresh_identity=True)
        self.assertTrue(result['associated'])
        self.assertGreater(result['device_id'], receipt['target'])
        self.assertFalse(result['controller_setup_complete'])
        self.assertEqual(len(self.transport.starts), starts + 1)
        self.assertGreater(self.store.get('sequence'), sequence)
        self.assertEqual(self.store.inventory()['groups'], inventory['groups'])
        self.assertEqual(self.store.inventory()['devices'][0], inventory['devices'][0])
        self.assertEqual({j['kind'] for j in self.store.journals()}, {'claim', 'claim_pre_key'})
        self.assertEqual(next(j for j in self.store.journals() if j['id'] == receipt['id']),
                         dict(receipt, status='retired'))

    async def test_invalid_receipt_fields_are_rejected_before_discovery(self):
        receipt = await self.failed_public_key()
        for location, field, value in invalid_profiles():
            # SQLite's INTEGER affinity normalizes floats and booleans. Strict
            # in-memory target types are covered by the profile unit tests.
            if location == 'journal':
                continue
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(receipt)
                if value is _MISSING:
                    bad['detail'].pop(field)
                else:
                    bad['detail'][field] = value
                self.replace_receipt(bad)
                await self.assert_rejected(receipt)
        self.replace_receipt(receipt)

    async def test_unisolated_claim_cannot_retire(self):
        receipt = await self.failed_public_key()
        self.runtime.claim_isolated.clear()
        await self.assert_rejected(receipt)

    async def test_ordinary_claim_capability_is_required(self):
        receipt = await self.failed_public_key()
        for value in (False, 1, 'true', None):
            with self.subTest(value=value):
                self.runtime.capabilities['ordinary_claim'] = value
                await self.assert_rejected(receipt)

    async def test_owner_reset_capability_is_not_needed_for_pre_key_retirement(self):
        receipt = await self.failed_public_key()
        self.runtime.capabilities['claim_reset_recovery'] = False
        before = len(self.transport.frames)
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertTrue(result['pre_key_retired'])
        self.assertFalse(result['reset_verified'])
        self.assert_only_discovery(before)

    async def test_tx_admission_cannot_be_bypassed(self):
        receipt = await self.failed_public_key()
        self.store.set('tx_admission', None)
        await self.assert_rejected(receipt)

    async def test_exhausted_sequence_cannot_be_bypassed(self):
        receipt = await self.failed_public_key()
        self.store.set('sequence', 0xffffff)
        await self.assert_rejected(receipt)
        self.assertEqual(self.store.get('sequence'), 0xffffff)

    async def test_competing_pending_operation_blocks_before_radio(self):
        receipt = await self.failed_public_key()
        self.store.begin('remove', 41001, {'synthetic': True})
        await self.assert_rejected(receipt)

    async def test_operation_lock_blocks_before_radio(self):
        receipt = await self.failed_public_key()
        async with self.runtime.operation:
            await self.assert_rejected(receipt)

    async def test_target_occupied_in_durable_inventory_even_if_runtime_is_stale(self):
        receipt = await self.failed_public_key()
        self.store.set('inventory', {'devices': [
            {'device_id': receipt['target'], 'name': 'Conflicting owner',
             'product_id': 93, 'exposed': False}], 'groups': []})
        self.assertNotIn(receipt['target'], self.runtime.core.devices)
        await self.assert_rejected(receipt)

    async def test_retired_target_cannot_be_retired_again(self):
        receipt = await self.failed_public_key()
        self.store.set('retired_addresses', [receipt['target']])
        await self.assert_rejected(receipt)

    async def test_target_owned_by_another_retained_receipt_is_not_exclusive(self):
        receipt = await self.failed_public_key()
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                              ('synthetic-target-alias', 'claim', receipt['target'],
                               'verified', json.dumps({'uuid': base.SECOND,
                                                       'uuid_hash': uuid_hash(bytes.fromhex(base.SECOND))})))
        await self.assert_rejected(receipt)

    async def test_foreign_uuid_alias_in_retained_history_blocks_recovery(self):
        receipt = await self.failed_public_key()
        other = copy.deepcopy(receipt['detail'])
        other['uuid'] = base.THIRD
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                              ('synthetic-hash-alias', 'claim', receipt['target'] + 10,
                               'retired', json.dumps(other)))
        self.store.set('retired_claims', ['synthetic-hash-alias'])
        self.store.set('retired_addresses', [receipt['target'] + 10])
        await self.assert_rejected(receipt)

    async def test_unretired_prior_same_uuid_is_not_safe_history(self):
        receipt = await self.failed_public_key()
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                              ('synthetic-unretired-history', 'claim', receipt['target'] + 10,
                               'verified', json.dumps(receipt['detail'])))
        await self.assert_rejected(receipt)

    async def test_tombstones_without_prior_matching_pre_key_proof_are_insufficient(self):
        receipt = await self.failed_public_key()
        prior_id, prior_target = 'synthetic-unproven-retirement', receipt['target'] + 10
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                              (prior_id, 'claim', prior_target, 'retired',
                               json.dumps(receipt['detail'])))
        self.store.set('retired_claims', [prior_id])
        self.store.set('retired_addresses', [prior_target])
        await self.assert_rejected(receipt)

    async def test_modified_prior_proof_does_not_authorize_later_retirement(self):
        first = await self.failed_public_key()
        await self.claims.reconcile_pre_key(first['id'])
        second = await self.failed_public_key()
        proof, = [j for j in self.store.journals() if j['kind'] == 'claim_pre_key']
        modified = copy.deepcopy(proof['detail'])
        modified['original_receipt']['detail']['name'] = 'Unmatched historical receipt'
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                              (json.dumps(modified), proof['id']))
        before = len(self.transport.frames)
        with self.assertRaises(ValueError):
            await self.claims.reconcile_pre_key(second['id'])
        self.assertEqual(len(self.transport.frames), before)
        self.assertEqual(next(j for j in self.store.journals() if j['id'] == second['id']), second)
        self.assertNotIn(second['target'], self.store.get('retired_addresses'))
        self.assertEqual(len([j for j in self.store.journals() if j['kind'] == 'claim_pre_key']), 1)

    async def test_proven_prior_retirement_of_same_uuid_is_safe_history(self):
        first = await self.failed_public_key()
        await self.claims.reconcile_pre_key(first['id'])
        second = await self.failed_public_key()
        self.assertGreater(second['target'], first['target'])
        before = len(self.transport.frames)
        result = await self.claims.reconcile_pre_key(second['id'])
        self.assertTrue(result['pre_key_retired'])
        self.assert_only_discovery(before)
        self.assertTrue({first['target'], second['target']} <= set(self.store.get('retired_addresses')))
        self.assertEqual(len([j for j in self.store.journals() if j['kind'] == 'claim_pre_key']), 2)

    async def test_missing_exact_unpaired_observation_preserves_receipt(self):
        receipt = await self.failed_public_key()
        self.transport.ads = [base.SECOND]
        before = len(self.transport.frames)
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertFalse(result['retired'])
        self.assert_only_discovery(before)
        self.assert_not_retired(receipt)

    async def test_hash_conflict_or_full_history_cannot_establish_proof(self):
        receipt = await self.failed_public_key()
        for key, value in (('unpaired_hash_conflicts', [FINGERPRINT]),
                           ('unpaired_history_full', True)):
            with self.subTest(key=key):
                self.store.set(key, value)
                before = len(self.transport.frames)
                result = await self.claims.reconcile_pre_key(receipt['id'])
                self.assertFalse(result['retired'])
                self.assert_only_discovery(before)
                self.assert_not_retired(receipt)
                self.store.set(key, [] if isinstance(value, list) else False)

    async def test_truncated_discovery_cannot_establish_proof(self):
        receipt = await self.failed_public_key()
        self.transport.ads = [base.IDENTITY] + [n.to_bytes(16, 'big').hex() for n in range(1, 130)]
        before = len(self.transport.frames)
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertFalse(result['retired'])
        self.assertTrue(self.claims.scan_truncated)
        self.assert_only_discovery(before)
        self.assert_not_retired(receipt)

    async def test_session_change_during_scan_preserves_receipt(self):
        receipt = await self.failed_public_key()
        self.transport.on_send = lambda _: self.runtime.session()
        await self.assert_rejected(receipt, radio=True)
        self.assertFalse(self.runtime.raw_observers)

    async def test_valid_but_changed_receipt_during_scan_cannot_commit(self):
        receipt = await self.failed_public_key()
        def changed(_):
            modified = copy.deepcopy(receipt)
            modified['detail']['name'] = 'Changed during observation'
            self.replace_receipt(modified)
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)

    async def test_inventory_metadata_race_during_scan_cannot_commit(self):
        await self.accepted_inventory()
        receipt = await self.failed_public_key()
        def changed(_):
            inventory = self.store.inventory()
            inventory['devices'][0]['name'] = 'Edited during observation'
            self.store.set('inventory', inventory)
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)
        self.assertEqual(self.store.inventory()['devices'][0]['name'], 'Edited during observation')

    async def test_group_membership_race_during_scan_cannot_commit(self):
        await self.accepted_inventory()
        receipt = await self.failed_public_key()
        def changed(_):
            inventory = self.store.inventory()
            inventory['groups'][0]['members'] = []
            self.store.set('inventory', inventory)
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)

    async def test_target_occupation_race_with_stale_runtime_cannot_commit(self):
        receipt = await self.failed_public_key()
        def changed(_):
            self.store.set('inventory', {'devices': [
                {'device_id': receipt['target'], 'name': 'Late owner',
                 'product_id': 93, 'exposed': False}], 'groups': []})
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)

    async def test_competing_receipt_arriving_during_scan_cannot_commit(self):
        receipt = await self.failed_public_key()
        def changed(_):
            self.store.begin('remove', 41001, {'synthetic': True})
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)

    async def test_isolation_removed_during_scan_cannot_commit(self):
        receipt = await self.failed_public_key()
        def changed(_):
            self.runtime.claim_isolated.clear()
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)

    async def test_capability_revoked_during_scan_cannot_commit(self):
        receipt = await self.failed_public_key()
        def changed(_):
            self.runtime.capabilities['ordinary_claim'] = False
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)

    async def test_tx_admission_revoked_during_scan_cannot_commit(self):
        receipt = await self.failed_public_key()
        def changed(_):
            self.store.set('tx_admission', None)
            return self.transport.ads
        self.transport.scan_rows = changed
        await self.assert_rejected(receipt, radio=True)

    async def test_cancellation_during_discovery_preserves_receipt_and_releases_guards(self):
        receipt = await self.failed_public_key()
        before = len(self.transport.frames)
        def cancelled(_):
            raise asyncio.CancelledError()
        self.transport.scan_rows = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.claims.recover_safe_pending(receipt['id'])
        self.assert_only_discovery(before)
        self.assert_not_retired(receipt)
        self.assertFalse(self.claims._safe_recovery_active)
        self.assertFalse(self.runtime.operation.locked())
        self.assertFalse(self.runtime.raw_observers)

    async def test_bounded_recovery_without_observation_never_restarts_association(self):
        receipt = await self.failed_public_key()
        self.transport.ads = []
        before = len(self.transport.frames)
        result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'unpaired_not_observed')
        self.assertEqual(result['ping_attempts'], 0)
        self.assert_only_discovery(before)
        self.assert_not_retired(receipt)

    async def test_bounded_recovery_requires_unchanged_receipt_while_reconnecting(self):
        receipt = await self.failed_public_key()
        self.runtime.mesh_ready = self.transport.ready = False
        before = len(self.transport.frames)
        async def changed(_):
            modified = copy.deepcopy(receipt)
            modified['detail']['name'] = 'Edited while waiting'
            self.replace_receipt(modified)
            self.ready_again()
        with patch('avionmesh_pi.commissioning.asyncio.sleep', side_effect=changed):
            result = await self.claims.recover_safe_pending(receipt['id'])
        self.assertEqual(result['outcome'], 'receipt_changed')
        self.assertEqual(result['ping_attempts'], 0)
        self.assertEqual(len(self.transport.frames), before)
        self.assert_not_retired(receipt)
