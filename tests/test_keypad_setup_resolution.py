"""Synthetic read-only partial setup closure; no physical model qualification."""
import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.keypad_ownership import owned_targets, partial_setup_slots, reservation
from avionmesh_pi.onboarding import KeypadOnboarding, table_from_rows
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_editor import EditorTransport
from test_service import KEY, INVENTORY


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        data = copy.deepcopy(INVENTORY)
        data['devices'][0].update(product_id=126, exposed=False)
        self.store.import_inventory(data)
        self.now, self.wakes, self.verbs = 10, [], []
        self.transport = EditorTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities = {'battery_onboarding': True, 'wired_onboarding': True,
                                     'battery_editor': True, 'wired_editor': True, 'inventory_complete': True}
        self.runtime.session()
        self.keypad = KeypadService(self.runtime, sleeper=self.sleep)
        self.setup = KeypadOnboarding(self.runtime, self.keypad)
        original_send = self.transport.send
        async def read_only(frame):
            p = decrypt_mcp(frame, KEY).payload
            self.verbs.append(p[0])
            self.assertIn(p[0], (1, 4), 'Recovery may only read keypad tables')
            await original_send(frame)
        self.transport.send = read_only
        self.before_rows = [bytes([1, 48, 0, slot, 255, 255, 0, 0]) for slot in range(1, 5)]
        self.desired_rows = [bytes([1, 48, 0, slot]) + (24576-slot).to_bytes(2, 'big') + b'\x08\xff'
                             for slot in range(1, 5)]
        self.groups = [reservation(40001, slot, 24576-slot) for slot in range(1, 5)]
        data['groups'] += self.groups
        detail = {'before': table_from_rows(self.before_rows), 'desired': table_from_rows(self.desired_rows),
                  'planned': [1, 2, 3, 4], 'attempted': [1, 2], 'verified': [1],
                  'stages': ['1/primary', '1/secondary', '2/primary', '2/secondary'],
                  'reservations': self.groups, 'product_id': 126, 'replies': {'1/primary': 5, '1/secondary': 1,
                                                                           '2/primary': 5, '2/secondary': None}}
        self.identifier = self.store.begin('keypad_setup', 40001, detail, inventory=data,
                                           reserved_targets=[g['group_id'] for g in self.groups])
        self.store.transition(self.identifier, 'prepared', 'attempted')
        self.store.transition(self.identifier, 'attempted', 'uncertain')
        self.transport.rows = [self.desired_rows[0]] + self.before_rows[1:]
        self.inventory = copy.deepcopy(self.store.inventory())
        self.original = copy.deepcopy(self.receipt())

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def sleep(self, seconds):
        self.now += round(seconds * 1000)
        if self.keypad.progress.get(40001) == 'settling':
            return
        def wake():
            self.wakes.append(self.now)
            self.runtime.observe(encrypt_mcp(b'\0\x80\x73\x0b\x1c' + bytes(5), KEY,
                                 self.transport.sequence, 40001), self.now)
            self.transport.sequence += 1
        asyncio.get_running_loop().call_soon(wake)

    def receipt(self):
        return next(row for row in self.store.journals() if row['id'] == self.identifier)

    def detail(self, **changes):
        current = self.receipt()
        current['detail'].update(changes)
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(current['detail']), self.identifier))

    async def resolve(self):
        return await self.setup.resolve_partial(self.identifier)

    async def test_partial_resolution_reads_twice_preserves_ownership_and_all_reservations(self):
        result = await self.resolve()
        self.assertEqual(result['applied_slots'], [1])
        self.assertEqual(result['unchanged_attempted_slots'], [2])
        self.assertEqual(result['unattempted_slots'], [3, 4])
        for flag in ('resolved', 'partial_resolution', 'assignment_read_back', 'reservations_retained'):
            self.assertIs(result[flag], True)
        for flag in ('write_retried', 'setup_automatically_resumed', 'exposure_changed', 'requires_review'):
            self.assertIs(result[flag], False)
        self.assertEqual(self.verbs, [4, 1, 1, 1, 1] * 2)
        self.assertEqual(len(self.wakes), 2)
        self.assertEqual(self.transport.writes, [])
        current = self.receipt()
        self.assertEqual(current['status'], 'cancelled')
        self.assertEqual({k: v for k, v in current['detail'].items() if k != 'partial_resolution'}, self.original['detail'])
        self.assertEqual(partial_setup_slots(current), [1])
        self.assertEqual(set(owned_targets(self.store, 40001)), {24575})
        self.assertEqual(self.store.inventory(), self.inventory)
        self.assertTrue(all(g in self.store.inventory()['groups'] for g in self.groups))
        self.assertTrue({g['group_id'] for g in self.groups} <= set(self.store.get('known_targets')))
        self.assertEqual(self.store.journals(pending=True), [])
        self.assertEqual(self.store.bindings()[40001], table_from_rows(self.transport.rows))
        self.assertEqual(self.keypad.battery_cache.status(40001)['status'], 'cached')

    async def test_new_ha_edit_allocates_fresh_target_and_keeps_resolved_button_owned(self):
        await self.resolve()
        current = self.store.bindings()[40001]
        plan, managed = self.keypad._plan(40001, current, 2, 'ha', None, None)
        self.assertNotIn(managed['new']['group_id'], [g['group_id'] for g in self.groups])
        self.assertEqual(set(owned_targets(self.store, 40001)), {24575})
        self.assertEqual(self.transport.writes, [])

    async def test_model127_uses_two_reads_without_battery_wakes(self):
        from dataclasses import replace
        self.runtime.core.devices[40001] = replace(self.runtime.core.devices[40001], product_id=127)
        data = self.store.inventory();data['devices'][0]['product_id'] = 127;self.store.set('inventory', data)
        self.detail(product_id=127)
        result = await self.resolve()
        self.assertEqual(result['applied_slots'], [1])
        self.assertEqual(len(self.verbs), 10)
        self.assertEqual(self.wakes, [])

    async def test_applied_but_unverified_attempt_is_recognized_without_continuing(self):
        self.transport.rows[1] = self.desired_rows[1]
        result = await self.resolve()
        self.assertEqual(result['applied_slots'], [1, 2])
        self.assertEqual(result['unchanged_attempted_slots'], [])
        self.assertEqual(result['unattempted_slots'], [3, 4])
        self.assertEqual(set(owned_targets(self.store, 40001)), {24575, 24574})
        self.assertEqual(self.transport.writes, [])

    async def test_all_unchanged_closes_without_granting_ownership(self):
        self.detail(verified=[])
        self.transport.rows = list(self.before_rows)
        result = await self.resolve()
        self.assertEqual(result['applied_slots'], [])
        self.assertEqual(result['unchanged_attempted_slots'], [1, 2])
        self.assertEqual(owned_targets(self.store, 40001), {})

    async def test_all_applied_still_never_auto_exposes_or_resumes(self):
        self.detail(attempted=[1, 2, 3, 4], stages=[f'{slot}/{kind}' for slot in range(1, 5) for kind in ('primary', 'secondary')])
        self.transport.rows = list(self.desired_rows)
        result = await self.resolve()
        self.assertEqual(result['applied_slots'], [1, 2, 3, 4])
        self.assertEqual(self.store.inventory(), self.inventory)
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assertFalse(result['setup_automatically_resumed'])

    async def test_verified_slot_regression_unknown_change_and_unattempted_assignment_refused(self):
        for slot, raw in ((0, self.before_rows[0]), (1, bytes.fromhex('01300002012c08ff')), (2, self.desired_rows[2])):
            with self.subTest(slot=slot):
                self.transport.rows = [self.desired_rows[0]] + self.before_rows[1:]
                self.transport.rows[slot] = raw
                with self.assertRaises(ValueError):
                    await self.resolve()
                self.assertEqual(self.receipt(), self.original)
                self.assertEqual(self.store.bindings(), {})
                self.assertEqual(self.transport.writes, [])

    async def test_disagreeing_rounds_never_close(self):
        original = self.keypad._round
        async def change(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.transport.rows[1] = self.desired_rows[1]
            return result
        with patch.object(self.keypad, '_round', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'disagree'):
                await self.resolve()
        self.assertEqual(self.receipt(), self.original)

    async def test_competing_pending_and_disabled_capability_refuse_before_radio(self):
        self.runtime.capabilities['battery_onboarding'] = False
        with self.assertRaises(ValueError):await self.resolve()
        self.runtime.capabilities['battery_onboarding'] = True
        self.store.begin('keypad_edit', 40002, {})
        with self.assertRaises(ValueError):await self.resolve()
        self.assertEqual(self.verbs, [])

    async def test_reservation_competition_and_retirement_refuse_before_reads(self):
        self.store.observe_target(24574, 40002)
        with self.assertRaises(ValueError):await self.resolve()
        self.store.set('target_sources', {})
        self.store.set('retired_addresses', [40001])
        with self.assertRaises(ValueError):await self.resolve()
        self.assertEqual(self.verbs, [])

    async def test_inventory_or_journal_change_during_reads_keeps_quarantine(self):
        original = self.keypad._round
        async def change(*args, **kwargs):
            result = await original(*args, **kwargs)
            data = self.store.inventory();data['devices'][0]['name'] = 'Changed while reading';self.store.set('inventory', data)
            return result
        with patch.object(self.keypad, '_round', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'authority changed'):
                await self.resolve()
        self.assertEqual(self.receipt(), self.original)
        self.assertEqual(self.transport.writes, [])

    async def test_cancelled_without_resolution_or_forged_resolution_never_grants_ownership(self):
        self.store.db.execute("UPDATE journals SET status='cancelled' WHERE id=?", (self.identifier,))
        self.assertEqual(owned_targets(self.store, 40001), {})
        self.store.db.execute("UPDATE journals SET status='uncertain' WHERE id=?", (self.identifier,))
        await self.resolve()
        valid = copy.deepcopy(self.receipt()['detail'])
        for field, value in [('applied_slots', [1, 2]), ('readback_rounds', 1), ('write_retried', True),
                             ('schema', 'untrusted'), ('assignment_read_back', False)]:
            with self.subTest(field=field):
                changed = copy.deepcopy(valid);changed['partial_resolution'][field] = value
                self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(changed), self.identifier))
                self.assertEqual(owned_targets(self.store, 40001), {})

    async def test_resolution_ownership_survives_restart_but_readback_freshness_does_not(self):
        await self.resolve()
        self.store.close();self.store = synthetic_store(self.temp.name, KEY)
        self.assertEqual(self.store.bindings(), {})
        self.assertEqual(set(owned_targets(self.store, 40001)), {24575})
        self.assertEqual(self.store.journals(pending=True), [])
        self.assertEqual(self.store.inventory(), self.inventory)

    async def test_malformed_history_is_rejected_before_any_radio_reads(self):
        valid = copy.deepcopy(self.receipt()['detail'])
        changes = [{'verified': [5]}, {'verified': [True]}, {'verified': '1'},
                   {'verified': [None]}, {'verified': [1, 1]}, {'before': {}},
                   {'desired': {'slots': []}}, {'attempted': [2, 1]}, {'stages': ['2/secondary']}]
        for change in changes:
            with self.subTest(change=change):
                altered = dict(copy.deepcopy(valid), **change)
                self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(altered), self.identifier))
                with self.assertRaises(ValueError):await self.resolve()
                self.assertEqual(self.verbs, [])
                self.assertEqual(self.receipt()['status'], 'uncertain')

    async def test_partial_owner_cannot_be_deleted_or_retyped_by_metadata_import(self):
        await self.resolve()
        for product in (None, 127, 134):
            with self.subTest(product=product):
                data = self.store.inventory()
                if product is None:data['devices'] = [d for d in data['devices'] if d['device_id'] != 40001]
                else:data['devices'][0]['product_id'] = product
                with self.assertRaisesRegex(ValueError, 'Managed keypad identity'):
                    self.store.import_inventory(data)
                self.assertEqual(self.store.inventory(), self.inventory)
        data = self.store.inventory();data['devices'][0]['name'] = 'Renamed keypad'
        self.store.import_inventory(data)
        self.assertEqual(set(owned_targets(self.store, 40001)), {24575})

    async def test_reload_retains_only_owned_cache_and_sharing_requires_fresh_readback(self):
        from avionmesh_pi.lifecycle import reload_inventory
        from avionmesh_pi.protocol import plan_light_command
        await self.resolve()
        count = len(self.verbs)
        reload_inventory(self.runtime)
        status = self.keypad.battery_cache.status(40001)
        self.assertEqual(status['status'], 'cached')
        self.assertEqual([row['slot'] for row in status['slots']], [1])
        self.assertFalse(status['assignment_read_back'])
        self.assertEqual(self.runtime.core.controllers.bindings, {})
        messages = [];self.runtime.sink = messages.append
        self.runtime.observe(encrypt_mcp(plan_light_command(24575, brightness=255), KEY, 10000, 40001), self.now)
        self.assertFalse(any('/input/button_' in m['topic'] and not m['retain'] for m in messages))
        self.assertEqual(len(self.verbs), count)
        data = self.store.inventory();data['devices'][0]['exposed'] = True
        self.store.set('inventory', data);reload_inventory(self.runtime)
        self.assertEqual(self.keypad.battery_cache.status(40001)['status'], 'revoked')
        await self.keypad.read(40001)
        self.runtime.session();messages.clear()
        self.runtime.observe(encrypt_mcp(plan_light_command(24575, brightness=255), KEY, 10001, 40001), self.now)
        events = [m['payload'] for m in messages if '/input/button_' in m['topic'] and not m['retain']]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['button'], 1)
        self.assertEqual(events[0]['button_mapping'], 'cached_table')
        self.assertEqual(self.transport.writes, [])

    async def test_repeat_resolution_is_refused_without_radio(self):
        await self.resolve();count = len(self.verbs)
        with self.assertRaises(ValueError):await self.resolve()
        self.assertEqual(len(self.verbs), count)

    async def test_read_cancellation_keeps_original_uncertainty(self):
        async def cancelled(*args, **kwargs):raise asyncio.CancelledError()
        with patch.object(self.keypad, 'verified_rounds', side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):await self.resolve()
        self.assertEqual(self.receipt(), self.original)
        self.assertEqual(self.store.bindings(), {})

    async def test_atomic_receipt_failure_rolls_back_binding_promotion(self):
        db = self.store.db
        class Failure:
            def execute(self, sql, *args):
                if "UPDATE journals SET status='cancelled'" in sql:raise sqlite3.OperationalError('synthetic failure')
                return db.execute(sql, *args)
            def __getattr__(self, name):return getattr(db, name)
        self.store.db = Failure()
        try:
            with self.assertRaises(sqlite3.OperationalError):await self.resolve()
        finally:self.store.db = db
        self.assertEqual(self.receipt(), self.original)
        self.assertEqual(self.store.bindings(), {})
        self.assertEqual(self.transport.writes, [])


if __name__ == '__main__':unittest.main()
