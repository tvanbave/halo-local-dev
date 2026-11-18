"""Synthetic wired-keypad editor behavior when a setter reply is missing.

Mirrors the frozen ESP editor: a reply is a hint, nothing is ever resent, and two
matching full-table reads decide what the keypad stored. Offline only; this does
not qualify physical keypad behavior.
"""
import copy
import tempfile
import unittest

from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.onboarding import KeypadOnboarding
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_editor import EditorTransport
from test_service import KEY, INVENTORY

DATA = copy.deepcopy(INVENTORY)
DATA['groups'].append({'group_id': 293, 'name': 'Second', 'members': [40002],
                       'exposed': True, 'protected': False})
ALLOWED = (127,)


class LossyTransport(EditorTransport):
    """Applies writes like the editor fixture but can withhold chosen replies."""

    def __init__(self):
        super().__init__()
        self.silent = set()          # 'delete', 'primary', 'secondary'
        self.ignore = set()          # setters the keypad never receives
        self.primary_commits = False  # keypad stores the record on the primary alone
        self.fail_reads_after = None

    async def send(self, frame):
        p = decrypt_mcp(frame, KEY).payload
        kind = None
        if p[1] == 48 and p[0] in (2, 5):
            kind = 'delete' if p[0] == 5 else 'secondary' if p[5] & 0x20 else 'primary'
        if kind in self.ignore:
            self.writes.append(p)
            return
        if kind in self.silent:
            observe = self.runtime.observe
            self.runtime.observe = lambda *a, **k: None
            try:
                await super().send(frame)
            finally:
                self.runtime.observe = observe
        else:
            await super().send(frame)
        if kind == 'primary' and self.primary_commits:
            self.rows[p[5] - 1], self.pending = self.pending, None
        if kind and kind == self.fail_reads_after:
            self.failed = True


class KeypadCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(DATA)
        self.transport = LossyTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        exchange = self.runtime.exchange

        def quick(plaintext, predicate, timeout=3, **options):
            return exchange(plaintext, predicate, timeout=min(timeout, .05), **options)
        self.runtime.exchange = quick
        self.editor = KeypadService(self.runtime)
        self.baseline = await self.editor.read(40001)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reconnect(self):
        self.transport.failed = False
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()

    def verbs(self):
        return [p[0] for p in self.transport.writes]

    async def edit(self, slot=1, group=291, level=200):
        table = self.runtime.core.controllers.bindings.get(40001) or self.baseline
        return await self.editor.apply(40001, slot, 'native', group, level,
                                       table['revision'], allowed_models=ALLOWED)


class LostReplyTests(KeypadCase):
    async def test_lost_secondary_reply_is_verified_by_readback_without_resend(self):
        self.transport.silent = {'secondary'}
        result = await self.edit()
        self.assertTrue(result['verified'])
        self.assertEqual(result['verified_without_reply'], ['insert_secondary'])
        self.assertEqual(self.verbs(), [5, 2, 2])
        journal = self.store.journals()[0]
        self.assertEqual(journal['status'], 'verified')
        self.assertEqual(journal['detail']['replies'], {'delete': 1, 'insert_primary': 5, 'insert_secondary': None})
        self.assertTrue(self.transport.ready)

    async def test_lost_delete_reply_continues_only_after_verified_removal(self):
        self.transport.silent = {'delete'}
        result = await self.edit()
        self.assertTrue(result['verified'])
        self.assertEqual(self.verbs(), [5, 2, 2])
        self.assertEqual(self.transport.rows[0].hex(), '01300001012308c8')

    async def test_unreceived_delete_stops_before_any_insertion(self):
        self.transport.ignore = {'delete'}
        with self.assertRaisesRegex(ValueError, 'Deletion did not reconcile'):
            await self.edit()
        self.assertEqual(self.verbs(), [5])
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_lost_primary_reply_never_sends_follow_up_or_repeat(self):
        self.transport.silent = {'primary'}
        with self.assertRaisesRegex(ValueError, 'does not match exact proposal'):
            await self.edit()
        self.assertEqual(self.verbs(), [5, 2])
        journal = self.store.journals()[0]
        self.assertEqual(journal['status'], 'uncertain')
        self.reconnect()
        result = await self.editor.reconcile(journal['id'])
        self.assertEqual(result, {'verified': False, 'requires_review': True, 'write_retried': False})
        with self.assertRaises(ValueError):
            await self.editor.resume_edit(journal['id'], allowed_models=ALLOWED)
        self.assertEqual(self.verbs(), [5, 2])

    async def test_primary_stored_without_reply_is_accepted_from_readback(self):
        self.transport.silent = {'primary'}
        self.transport.primary_commits = True
        result = await self.edit()
        self.assertTrue(result['verified'])
        self.assertEqual(result['verified_without_reply'], ['insert_primary'])
        self.assertEqual(self.verbs(), [5, 2])

    async def test_unexpected_primary_status_skips_follow_up(self):
        send = self.transport.send

        async def odd_status(frame):
            p = decrypt_mcp(frame, KEY).payload
            if p[1] == 48 and p[0] == 2 and not p[5] & 0x20:
                self.transport.primary_commits = True
            await send(frame)
        self.transport.send = odd_status
        observe = self.runtime.observe

        def rewrite(frame, when):
            packet = decrypt_mcp(frame, KEY)
            if packet.payload[:2] == b'\x02\x30' and len(packet.payload) == 4 and packet.payload[3] == 5:
                frame = encrypt_mcp(b'\x00\x80\x73' + packet.payload[:3] + b'\x01', KEY,
                                    packet.sequence, packet.crypto_source)
            return observe(frame, when)
        self.runtime.observe = rewrite
        result = await self.edit()
        self.assertTrue(result['verified'])
        self.assertEqual(self.verbs(), [5, 2])
        self.assertEqual(self.store.journals()[0]['detail']['replies']['insert_primary'], 1)

    async def test_reconcile_closes_receipt_when_original_table_is_unchanged(self):
        self.transport.ignore = {'primary'}
        with self.assertRaises(ValueError):
            await self.edit(slot=2, group=293)
        journal = self.store.journals()[0]
        self.assertEqual(journal['status'], 'uncertain')
        self.reconnect()
        result = await self.editor.reconcile(journal['id'])
        self.assertEqual(result, {'verified': False, 'unchanged': True, 'requires_review': False,
                                  'write_retried': False})
        closed = self.store.journals()[0]
        self.assertEqual(closed['status'], 'cancelled')
        self.assertEqual(closed['detail']['resolution'], 'original_table_verified_unchanged')
        self.assertEqual(self.store.journals(pending=True), [])
        self.assertEqual(self.runtime.core.controllers.bindings[40001], self.baseline)
        self.assertEqual(self.verbs(), [2])
        # A later edit is a new receipt with its own complete verification.
        self.transport.ignore = set()
        self.assertTrue((await self.edit(slot=2, group=293))['verified'])
        self.assertEqual(len(self.store.journals()), 2)

    async def test_reconcile_records_verified_removal_for_explicit_resume(self):
        self.transport.silent = {'delete'}
        self.transport.fail_reads_after = 'delete'
        with self.assertRaises(RuntimeError):
            await self.edit()
        journal = self.store.journals()[0]
        self.assertEqual(journal['detail']['stages'], ['delete'])
        self.assertNotIn('delete_checkpoint', journal['detail'])
        self.transport.silent, self.transport.fail_reads_after = set(), None
        self.reconnect()
        result = await self.editor.reconcile(journal['id'])
        self.assertEqual(result, {'verified': False, 'requires_review': True, 'resumable': True,
                                  'write_retried': False})
        self.assertEqual(self.verbs(), [5])
        resumed = await self.editor.resume_edit(journal['id'], allowed_models=ALLOWED)
        self.assertTrue(resumed['verified'])
        self.assertEqual(self.verbs(), [5, 2, 2])

    async def test_reconcile_closes_requested_unassignment_after_lost_postdelete_read(self):
        self.transport.silent = {'delete'}
        self.transport.fail_reads_after = 'delete'
        with self.assertRaises(RuntimeError):
            await self.editor.apply(40001, 1, 'unassigned', None, None,
                                    self.baseline['revision'], allowed_models=ALLOWED)
        journal = self.store.journals()[0]
        self.assertEqual(journal['status'], 'uncertain')
        self.assertEqual(journal['detail']['stages'], ['delete'])
        self.assertNotIn('delete_checkpoint', journal['detail'])
        self.assertEqual(journal['detail']['desired']['slots'][0]['target'], 65535)
        self.transport.silent, self.transport.fail_reads_after = set(), None
        self.reconnect()
        writes = list(self.transport.writes)
        result = await self.editor.reconcile(journal['id'])
        self.assertEqual(result, {'verified': True, 'write_retried': False})
        self.assertEqual(self.transport.writes, writes)
        self.assertEqual(self.verbs(), [5])
        self.assertEqual(self.store.journals()[0]['status'], 'verified')
        self.assertEqual(self.store.journals(pending=True), [])
        self.assertEqual(self.store.bindings()[40001], journal['detail']['desired'])
        self.assertEqual(self.runtime.core.controllers.bindings[40001], journal['detail']['desired'])

    async def test_onboarding_lost_secondary_reply_completes_from_readback(self):
        self.runtime.capabilities = {'wired_onboarding': True, 'inventory_complete': True}
        self.transport.silent = {'secondary'}
        result = await KeypadOnboarding(self.runtime, self.editor).setup(40001)
        self.assertTrue(result['setup_complete'])
        self.assertEqual(self.verbs(), [2, 2] * 3)
        self.assertEqual(self.store.journals()[0]['status'], 'verified')


class SaveManyTests(KeypadCase):
    async def save(self, changes):
        table = self.runtime.core.controllers.bindings[40001]
        return await self.editor.apply_many(40001, changes, table['revision'], allowed_models=ALLOWED)

    async def test_several_buttons_are_saved_one_verified_edit_at_a_time(self):
        result = await self.save([{'slot': 2, 'mode': 'native', 'group': 293},
                                  {'slot': 1, 'mode': 'native', 'group': 291, 'brightness': 128}])
        self.assertEqual(result['buttons'], [1, 2])
        self.assertTrue(result['verified'])
        self.assertEqual(self.transport.rows[0].hex(), '0130000101230880')
        self.assertEqual(self.transport.rows[1].hex(), '01300002012508ff')
        self.assertEqual([j['status'] for j in self.store.journals()], ['verified', 'verified'])
        self.assertEqual(self.editor.saves[40001],
                         {'state': 'verified', 'buttons': [{'slot': 1, 'state': 'verified'},
                                                           {'slot': 2, 'state': 'verified'}]})
        self.assertEqual(result['revision'], self.runtime.core.controllers.bindings[40001]['revision'])

    async def test_group_can_move_between_buttons_because_removals_run_first(self):
        result = await self.save([{'slot': 2, 'mode': 'native', 'group': 291},
                                  {'slot': 1, 'mode': 'unassigned'}])
        self.assertEqual(result['buttons'], [1, 2])
        self.assertEqual(self.verbs(), [5, 2, 2])
        self.assertEqual(self.transport.rows[0].hex(), '01300001ffff0000')
        self.assertEqual(self.transport.rows[1].hex(), '01300002012308ff')

    async def test_invalid_sets_are_refused_before_any_radio_use(self):
        bad = ([{'slot': 2, 'mode': 'native', 'group': 291}],             # duplicate target
               [{'slot': 2, 'mode': 'native', 'group': 292}],             # protected group
               [{'slot': 2, 'mode': 'native', 'group': 293}, {'slot': 2, 'mode': 'unassigned'}],
               [{'slot': 2, 'mode': 'native', 'group': 293, 'brightness': 1.0}],
               [{'slot': 2, 'mode': 'native', 'group': 293, 'raw': '00'}],
               [{'slot': 5, 'mode': 'unassigned'}], [], 'x')
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.save(changes)
        with self.assertRaises(ValueError):
            await self.editor.apply_many(40001, [{'slot': 2, 'mode': 'native', 'group': 293}],
                                         'stale', allowed_models=ALLOWED)
        with self.assertRaises(ValueError):
            await self.editor.apply_many(40001, [{'slot': 2, 'mode': 'native', 'group': 293}],
                                         self.baseline['revision'])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])

    async def test_unchanged_selection_sends_nothing(self):
        result = await self.save([{'slot': 1, 'mode': 'native', 'group': 291}])
        self.assertEqual(result, {'changed': False, 'verified': True, 'buttons': [],
                                  'revision': self.baseline['revision']})
        self.assertEqual(self.transport.writes, [])

    async def test_failure_stops_before_later_buttons(self):
        self.transport.ignore = {'delete'}
        with self.assertRaises(ValueError):
            await self.save([{'slot': 1, 'mode': 'unassigned'},
                             {'slot': 2, 'mode': 'native', 'group': 293}])
        self.assertEqual(self.verbs(), [5])
        self.assertEqual(self.editor.saves[40001],
                         {'state': 'needs_review', 'buttons': [{'slot': 1, 'state': 'needs_review'},
                                                               {'slot': 2, 'state': 'not_started'}]})
        self.assertEqual(len(self.store.journals()), 1)


if __name__ == '__main__':
    unittest.main()
