"""Owner-declared reset proof for only an empty direct-first-device mesh."""
import json
from types import SimpleNamespace
import unittest

import test_association as base
from test_bootstrap_relay import BootstrapTransport

IDENTITY = bytes.fromhex('000102030405140000040a0b0c0d0e0f')
ADDRESS = '05:04:03:02:01:00'


class BootstrapResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.ClaimTests.asyncSetUp(self)
        self.runtime.bootstrap = SimpleNamespace(needs_first_claim=True, selected_address=ADDRESS)
        self.transport = BootstrapTransport(self.runtime, 'without_final_ack', IDENTITY)
        self.runtime.transport = self.transport
        await self.claims.discover(seconds=0)
        with self.assertRaises(RuntimeError):
            await self.claims.claim(IDENTITY.hex(), 'First RL4', 93, inventory_complete=True)
        self.receipt, = self.store.journals()
        self.transport.disconnect_at = None
        self.runtime.mesh_ready = self.transport.ready = True
        self.runtime.session()
        original = self.claims.discover
        async def fast():
            return await original(seconds=0)
        self.claims.discover = fast

    asyncTearDown = base.ClaimTests.asyncTearDown

    def change_detail(self, **changes):
        detail = dict(self.receipt['detail'], **changes)
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), self.receipt['id']))

    def group(self):
        self.store.set('inventory', {'devices': [], 'groups': [
            {'group_id':291,'name':'Existing','members':[],'protected':False,'exposed':False}]})

    def accepted_history(self):
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                             ('older', 'claim', 40000, 'verified', '{}'))

    async def assert_refused_without_dispatch(self, owner=True):
        before = len(self.transport.frames)
        with self.assertRaises(ValueError):
            await self.claims.reconcile_owner_reset(self.receipt['id'], owner)
        self.assertEqual(len(self.transport.frames), before)
        current = next(j for j in self.store.journals() if j['id'] == self.receipt['id'])
        self.assertEqual(current['status'], 'uncertain')
        self.assertEqual(self.store.get('retired_addresses', []), [])

    async def test_owner_reset_retires_only_then_manual_reclaim_uses_new_address(self):
        before = len(self.transport.frames)
        result = await self.claims.reconcile_owner_reset(self.receipt['id'], True)
        self.assertTrue(result['retired'])
        self.assertFalse(result['automatic_reclaim'])
        self.assertFalse(result['association_retried'])
        self.assertEqual(len(self.transport.frames), before + 1)  # discovery only
        self.assertEqual(self.transport.association_starts, 1)
        self.assertEqual(self.store.inventory()['devices'], [])
        self.assertIn(self.receipt['target'], self.store.get('retired_addresses'))
        proof = next(j for j in self.store.journals() if j['kind'] == 'claim_reset')
        self.assertFalse(proof['detail']['reset_dispatched'])
        self.assertTrue(proof['detail']['owner_reset_declared'])
        # The owner now separately initiates a new claim; new synthetic peer
        # represents reset hardware and must not reuse failed reserved address.
        self.transport.peer = base.Peer()
        await self.claims.discover()
        claimed = await self.claims.claim(IDENTITY.hex(), 'Retry after owner reset', 93, inventory_complete=True)
        self.assertTrue(claimed['associated'])
        self.assertGreater(claimed['device_id'], self.receipt['target'])
        self.assertEqual(self.transport.association_starts, 2)

    async def test_no_owner_declaration_is_rejected(self):
        for value in (False, None, 1):
            await self.assert_refused_without_dispatch(value)

    async def test_runtime_bootstrap_required(self):
        self.runtime.bootstrap = None
        await self.assert_refused_without_dispatch()

    async def test_selected_mac_must_match(self):
        self.runtime.bootstrap.selected_address = '05:04:03:02:01:01'
        await self.assert_refused_without_dispatch()

    async def test_retained_profile_must_match(self):
        self.change_detail(uuid=base.IDENTITY.hex())
        await self.assert_refused_without_dispatch()

    async def test_existing_groups_exclude_reset(self):
        self.group()
        await self.assert_refused_without_dispatch()

    async def test_accepted_claim_history_excludes_reset(self):
        self.accepted_history()
        await self.assert_refused_without_dispatch()

    async def test_scope_changes_during_scan_cannot_retire(self):
        changes = [lambda: setattr(self.runtime, 'bootstrap', None),
                   lambda: setattr(self.runtime.bootstrap, 'needs_first_claim', False),
                   lambda: setattr(self.runtime.bootstrap, 'selected_address', '05:04:03:02:01:01'),
                   self.group, self.accepted_history]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                await self.asyncTearDown()
                await self.asyncSetUp()
                scan = self.claims.discover
                async def changed_scan():
                    result = await scan()
                    change()
                    return result
                self.claims.discover = changed_scan
                with self.assertRaises(ValueError):
                    await self.claims.reconcile_owner_reset(self.receipt['id'], True)
                current = next(j for j in self.store.journals() if j['id'] == self.receipt['id'])
                self.assertEqual(current['status'], 'uncertain')
                self.assertEqual(self.store.get('retired_addresses', []), [])
                self.assertEqual(self.transport.association_starts, 1)

    async def test_existing_mesh_admission_excludes_reset(self):
        receipt = self.store.get('tx_admission')
        receipt['document']['mode'] = 'verified_history'
        self.store.set('tx_admission', receipt)
        await self.assert_refused_without_dispatch()
