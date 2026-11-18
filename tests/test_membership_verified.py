"""Synthetic RL4 membership contracts, failures, and bounded batch behavior."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from avionmesh_pi.membership import Membership
from avionmesh_pi.management import recovery_advice
from avionmesh_pi.lifecycle import reload_inventory
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from support import synthetic_store as Store
import test_service as service

KEY = service.KEY


class GroupTransport(service.FakeTransport):
    def __init__(self):
        super().__init__()
        self.tables = {40002: [291, 0, 0, 0], 40003: [0, 0, 0, 0]}
        self.mutations = []
        self.on_count = None
        self.before_reply = None
        self.group_counts = 0
        self.fail_after_write = False
        self.ignore_write = False
        self.missing_target = None

    async def send(self, frame):
        p = decrypt_mcp(frame, KEY)
        if p.payload[1] != 3:
            return await super().send(frame)
        self.frames.append(frame)
        target, payload = p.mcp_address, p.payload
        table = self.tables[target]
        if payload[0] in (2, 5):
            group = int.from_bytes(payload[5:7], 'big')
            self.mutations.append((target, group, payload[0] == 2))
            if not self.ignore_write:
                if payload[0] == 2 and group not in table:
                    table[table.index(0)] = group
                if payload[0] == 5 and group in table:
                    table[table.index(group)] = 0
            if self.fail_after_write:
                raise RuntimeError('synthetic loss after physical change')
            return
        if payload[0] == 4:
            self.group_counts += 1
            if self.on_count:
                self.on_count(self.group_counts)
            raw = bytes([4, 3, 0, sum(value != 0 for value in table), len(table)])
        else:
            slot = payload[5]
            raw = bytes([1, 3, 0, slot]) + table[slot - 1].to_bytes(2, 'big')
        if self.before_reply:
            self.before_reply(target, raw)
        if target == self.missing_target:
            raise asyncio.TimeoutError('synthetic missing reply')
        self.reply(target, raw)

    def reply(self, target, payload, *, sequence=None):
        sequence = self.sequence if sequence is None else sequence
        self.sequence += 1
        frame = encrypt_mcp(b'\x00\x80\x73' + payload, KEY, sequence, target)
        self.receiver(frame, 10)


class VerifiedMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await service.RuntimeTests.asyncSetUp(self)
        data = self.store.inventory()
        data['devices'][1]['product_id'] = 93
        data['devices'].append(dict(data['devices'][1], device_id=40003, name='Second RL4'))
        data['groups'].append({'group_id': 293, 'name': 'Other room', 'members': [], 'exposed': False, 'protected': False})
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        self.transport = GroupTransport()
        self.transport.receiver = self.runtime.observe
        self.runtime.transport = self.transport
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        self.member = Membership(self.runtime)

    asyncTearDown = service.RuntimeTests.asyncTearDown

    def group(self, identifier=291):
        return next(g for g in self.store.inventory()['groups'] if g['group_id'] == identifier)

    async def test_two_reads_accept_sparse_table_and_do_not_change_metadata(self):
        self.transport.tables[40002] = [0, 293, 0, 291]
        before = self.store.inventory()
        result = await self.member.read(40002)
        self.assertEqual(result['table'], {'used': 2, 'capacity': 4, 'groups': [291, 293]})
        self.assertEqual(before, self.store.inventory())
        self.assertEqual(self.transport.mutations, [])
        packets = [decrypt_mcp(frame, KEY).payload for frame in self.transport.frames]
        self.assertEqual(packets[0].hex(), '04030000000000000000')
        self.assertEqual(packets[1].hex(), '01030000000100000000')

    async def test_already_matching_table_never_rewrites(self):
        self.group()['members']  # selected group is already present physically
        result = await self.member.change(40002, 291, True)
        self.assertTrue(result['desired_matched'])
        self.assertIsNone(result['transaction'])
        self.assertEqual(self.transport.mutations, [])

    async def test_add_and_delete_require_postwrite_reads_and_save_receipt(self):
        added = await self.member.change(40003, 291, True)
        self.assertTrue(added['membership_verified'])
        self.assertEqual(self.group()['members'], [40002, 40003])
        deleted = await self.member.change(40002, 291, False)
        self.assertTrue(deleted['desired_matched'])
        self.assertEqual(self.group()['members'], [40003])
        self.assertEqual(self.transport.mutations, [(40003, 291, True), (40002, 291, False)])
        self.assertTrue(all(j['status'] == 'verified' for j in self.store.journals()))
        self.assertEqual(self.runtime.quarantined_groups(), set())

    async def test_noop_corrects_stale_metadata_only_from_verified_table(self):
        self.transport.tables[40003] = [291, 0, 0, 0]
        result = await self.member.change(40003, 291, True)
        self.assertTrue(result['metadata_changed'])
        self.assertEqual(self.group()['members'], [40002, 40003])
        self.assertEqual(self.transport.mutations, [])

    async def test_failed_submission_retains_uncertainty_then_read_only_reconcile(self):
        self.transport.fail_after_write = True
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        journal = self.store.journals(pending=True)[0]
        self.assertEqual(journal['status'], 'uncertain')
        self.assertEqual(self.group()['members'], [40002])
        self.assertEqual(self.runtime.quarantined_groups(), {291})
        self.assertEqual(recovery_advice(journal)['actions'], ['reconcile'])
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        result = await self.member.reconcile(journal['id'])
        self.assertTrue(result['desired_matched'])
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])
        self.assertEqual(self.group()['members'], [40002, 40003])

    async def test_unchanged_readback_reports_not_applied_without_retry(self):
        self.transport.ignore_write = True
        result = await self.member.change(40003, 291, True)
        self.assertTrue(result['membership_verified'])
        self.assertFalse(result['desired_matched'])
        self.assertEqual(self.group()['members'], [40002])
        self.assertEqual(len(self.transport.mutations), 1)

    async def test_full_table_refuses_add_without_journal_or_mutation(self):
        self.transport.tables[40003] = [293]
        with self.assertRaises(ValueError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.store.journals(), [])
        self.assertEqual(self.transport.mutations, [])

    async def test_different_rounds_refuse_before_write(self):
        self.transport.on_count = lambda count: self.transport.tables[40003].__setitem__(0, 293) if count == 2 else None
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.store.journals(), [])
        self.assertEqual(self.transport.mutations, [])

    async def test_duplicate_and_invalid_groups_refuse(self):
        for table in ([293, 293], [1]):
            self.transport.tables[40003] = table
            with self.assertRaises(ValueError):
                await self.member.read(40003)
        self.assertEqual(self.transport.mutations, [])

    async def test_incomplete_table_is_not_empty(self):
        original = self.transport.reply
        def corrupt(target, payload, **kwargs):
            if payload[0] == 4:
                payload = bytes([4, 3, 0, 1, 4])
            original(target, payload, **kwargs)
        self.transport.reply = corrupt
        with self.assertRaises(ValueError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.transport.mutations, [])

    async def test_session_change_invalidates_read(self):
        self.transport.before_reply = lambda target, raw: self.runtime.session()
        with self.assertRaises(RuntimeError):
            await self.member.read(40003)
        self.assertEqual(self.transport.mutations, [])

    async def test_missing_reply_never_authorizes_change(self):
        self.transport.missing_target = 40003
        with self.assertRaises(asyncio.TimeoutError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.transport.mutations, [])

    async def test_inventory_change_during_read_prevents_write(self):
        def edit(count):
            if count == 2:
                data = self.store.inventory()
                data['groups'][0]['name'] = 'Concurrent edit'
                self.store.set('inventory', data)
        self.transport.on_count = edit
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.transport.mutations, [])
        self.assertEqual(self.group()['name'], 'Concurrent edit')

    async def test_foreign_and_stale_sequence_replies_cannot_satisfy_read(self):
        def inject(target, raw):
            self.transport.reply(40001, raw)
            self.transport.reply(target, raw, sequence=1)
        self.transport.before_reply = inject
        result = await self.member.read(40002)
        self.assertEqual(result['table']['groups'], [291])

    async def test_capability_model_protected_and_busy_gates(self):
        for args in [(40001, 291, True), (40003, 292, True)]:
            with self.assertRaises(ValueError):
                await self.member.change(*args)
        self.runtime.capabilities['group_membership_readback'] = False
        with self.assertRaises(ValueError):
            await self.member.read(40003)
        self.runtime.capabilities['group_membership_readback'] = True
        async with self.runtime.operation:
            with self.assertRaises(RuntimeError):
                await self.member.read(40003)
        self.assertEqual(self.transport.frames, [])

    async def test_batch_prevalidates_all_targets_and_rejects_duplicates(self):
        for targets in ([40003, 40001], [40003, 40003], [], [True]):
            with self.assertRaises(ValueError):
                await self.member.change_many(targets, 291, True)
        self.assertEqual(self.transport.frames, [])

    async def test_batch_preserves_completed_rows_and_stops_on_uncertainty(self):
        self.transport.tables[40002] = [0, 0, 0, 0]
        self.transport.missing_target = 40003
        result = await self.member.change_many([40002, 40003], 291, True)
        self.assertFalse(result['complete'])
        self.assertTrue(result['results'][0]['desired_matched'])
        self.assertFalse(result['results'][1]['membership_verified'])
        self.assertEqual(self.transport.mutations, [(40002, 291, True)])
        self.assertEqual(self.store.journals()[0]['status'], 'verified')

    async def test_batch_success_changes_only_selected_targets(self):
        result = await self.member.change_many([40003], 291, True)
        self.assertTrue(result['complete'])
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])
        self.assertEqual(self.group(293)['members'], [])

    async def test_batch_stops_when_write_not_observed(self):
        self.transport.ignore_write = True
        result = await self.member.change_many([40003, 40002], 291, True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['remaining'], [40002])
        self.assertEqual(len(self.transport.mutations), 1)

    async def test_restart_reconcile_never_repeats_mutation(self):
        self.transport.fail_after_write = True
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        identifier = self.store.journals(pending=True)[0]['id']
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.runtime.store = self.store
        result = await self.member.reconcile(identifier)
        self.assertTrue(result['membership_verified'])
        self.assertEqual(len(self.transport.mutations), 1)

    async def test_storage_failure_rolls_back_membership_and_keeps_receipt(self):
        original = self.store.set
        def fail(key, value):
            if key == 'inventory':
                raise RuntimeError('synthetic storage failure')
            return original(key, value)
        with patch.object(self.store, 'set', side_effect=fail):
            with self.assertRaises(RuntimeError):
                await self.member.change(40003, 291, True)
        self.assertEqual(self.group()['members'], [40002])
        self.assertEqual(self.store.journals(pending=True)[0]['status'], 'uncertain')

    async def test_null_group_or_member_refused_before_any_read_or_journal(self):
        for group, member in [(None, True), (291, None), (None, None)]:
            with self.assertRaises(ValueError):
                await self.member.change(40003, group, member)
            with self.assertRaises(ValueError):
                await self.member.change_many([40003], group, member)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def test_reserved_enrollment_group_refused_even_if_metadata_unprotected(self):
        data = self.store.inventory()
        data['groups'].append({'group_id':24566,'name':'Untrusted metadata','members':[],
                               'exposed':False,'protected':False})
        self.store.set('inventory', data)
        for verified in (True, False):
            self.runtime.capabilities['group_membership_readback'] = verified
            with self.assertRaises(ValueError):
                await self.member.change(40003, 24566, True)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def test_collateral_group_change_keeps_quarantine_through_reconcile(self):
        self.transport.tables[40003] = [293, 0, 0, 0]
        def collateral(count):
            if count == 3:
                self.transport.tables[40003][0] = 0
        self.transport.on_count = collateral
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        journal = self.store.journals(pending=True)[0]
        self.assertEqual(journal['status'], 'uncertain')
        with self.assertRaises(RuntimeError):
            await self.member.reconcile(journal['id'])
        self.assertEqual(self.group()['members'], [40002])
        self.assertEqual(len(self.transport.mutations), 1)
        self.assertEqual(self.runtime.quarantined_groups(), {291})

    async def test_changed_capacity_keeps_transaction_uncertain(self):
        def shrink(count):
            if count == 3:
                self.transport.tables[40003].pop()
        self.transport.on_count = shrink
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.store.journals(pending=True)[0]['status'], 'uncertain')
        self.assertEqual(self.group()['members'], [40002])

    async def test_reconcile_remains_available_when_writes_are_disabled(self):
        self.transport.fail_after_write = True
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        identifier = self.store.journals(pending=True)[0]['id']
        self.runtime.capabilities['group_membership'] = False
        result = await self.member.reconcile(identifier)
        self.assertTrue(result['desired_matched'])
        self.assertEqual(len(self.transport.mutations), 1)

    def test_cpp_request_fixture_matches_python(self):
        import json
        from pathlib import Path
        from avionmesh_pi.protocol import model_command
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/esp-membership-requests.json').read_text())
        expected = [model_command(40002, 1 if slot else 4, 3, bytes([slot, 0, 0])).hex() for slot in range(65)]
        self.assertEqual(fixture['wire_plaintexts'], expected)

    async def test_pre_dispatch_publication_failure_is_reconcilable(self):
        with patch.object(self.runtime, 'session', side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaises(RuntimeError):
                await self.member.change(40003, 291, True)
        journal = self.store.journals(pending=True)[0]
        self.assertEqual(journal['status'], 'uncertain')
        self.assertEqual(self.transport.mutations, [])
        result = await self.member.reconcile(journal['id'])
        self.assertTrue(result['membership_verified'])
        self.assertFalse(result['desired_matched'])
        self.assertEqual(self.transport.mutations, [])

    async def test_cancel_after_mutation_preserves_receipt_without_retry(self):
        original = self.transport.send
        changed = asyncio.Event()
        async def hang_after_write(frame):
            await original(frame)
            packet = decrypt_mcp(frame, KEY)
            if packet.payload[:2] == b'\x02\x03':
                changed.set()
                await asyncio.Event().wait()
        self.transport.send = hang_after_write
        task = asyncio.create_task(self.member.change(40003, 291, True))
        await changed.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        journal = self.store.journals(pending=True)[0]
        self.assertEqual(journal['status'], 'uncertain')
        self.assertFalse(self.runtime.operation.locked())
        self.transport.send = original
        result = await self.member.reconcile(journal['id'])
        self.assertTrue(result['desired_matched'])
        self.assertEqual(len(self.transport.mutations), 1)

    async def test_session_change_after_mutation_does_not_commit_observation(self):
        self.transport.on_count = lambda count: self.runtime.session() if count == 3 else None
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 291, True)
        self.assertEqual(self.store.journals(pending=True)[0]['status'], 'uncertain')
        self.assertEqual(self.group()['members'], [40002])
        self.assertEqual(len(self.transport.mutations), 1)

    async def test_postcommit_publication_failure_preserves_verified_batch_result(self):
        original=self.runtime.emit
        def fail_after_commit(messages):
            if any(j['kind']=='group_membership' and j['status']=='verified' for j in self.store.journals()):
                raise RuntimeError('synthetic publication failure after commit')
            return original(messages)
        with patch.object(self.runtime,'emit',side_effect=fail_after_commit):
            result=await self.member.change_many([40003,40002],291,True)
        self.assertFalse(result['complete'])
        self.assertTrue(result['results'][0]['membership_verified'])
        self.assertTrue(result['results'][0]['desired_matched'])
        self.assertTrue(result['results'][0]['publication_pending'])
        self.assertEqual(result['remaining'],[40002])
        self.assertEqual(self.group()['members'],[40002,40003])
        self.assertEqual(len(self.transport.mutations),1)
