"""Per-button HA routes use synthetic transport and private disposable state only."""
import copy
import unittest

from avionmesh_pi.controller_workflow import controller_workflows
from avionmesh_pi.keypad import BackgroundReadDeferred, replace_slot
from avionmesh_pi.keypad_ownership import owned_targets
from avionmesh_pi.onboarding import KeypadOnboarding
from test_keypad_lost_reply import KeypadCase


class ManagedHaEditTests(KeypadCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.runtime.capabilities = {'wired_editor': True, 'inventory_complete': True}

    async def save(self, changes):
        table = self.runtime.core.controllers.bindings[40001]
        return await self.editor.apply_many(40001, changes, table['revision'], allowed_models=(127,))

    def view(self):
        return next(v for v in controller_workflows(self.runtime) if v['device_id'] == 40001)

    async def test_native_to_ha_changes_only_selected_button_and_preserves_sharing(self):
        inventory = self.store.inventory()
        self.assertTrue((await self.save([{'slot': 1, 'mode': 'ha'}]))['verified'])
        actual = self.runtime.core.controllers.bindings[40001]
        self.assertEqual(actual['slots'][1:], self.baseline['slots'][1:])
        self.assertEqual(actual['slots'][0]['target'], 24575)
        self.assertEqual(actual['slots'][0]['level'], 255)
        self.assertEqual(self.verbs(), [5, 2, 2])
        after = self.store.inventory()
        self.assertEqual(after['devices'], inventory['devices'])
        self.assertEqual(after['groups'][:-1], inventory['groups'])
        self.assertEqual(after['groups'][-1]['members'], [])
        self.assertFalse(after['groups'][-1]['exposed'])
        self.assertEqual(self.view()['slots'][0]['assignment_mode'], 'ha')
        self.assertTrue(self.view()['slots'][0]['ha_owned'])
        self.assertTrue(self.view()['slots'][0]['editable'])

    async def test_owned_ha_can_return_to_native_or_unused_without_recycling_target(self):
        await self.save([{'slot': 1, 'mode': 'ha'}])
        first = copy.deepcopy(self.store.inventory()['groups'][-1])
        await self.save([{'slot': 1, 'mode': 'native', 'group': 291, 'brightness': 128}])
        self.assertEqual(self.view()['slots'][0]['assignment_mode'], 'native')
        self.assertNotIn(first['group_id'], owned_targets(self.store, 40001))
        self.assertIn(first, self.store.inventory()['groups'])
        await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertEqual(self.view()['slots'][0]['target'], 24574)
        await self.save([{'slot': 1, 'mode': 'unassigned'}])
        self.assertEqual(self.view()['slots'][0]['assignment_mode'], 'unassigned')
        self.assertEqual(len([g for g in self.store.inventory()['groups'] if g['group_id'] in (24574, 24575)]), 2)
        self.assertTrue({24574, 24575}.issubset(set(self.store.get('known_targets'))))

    async def test_ha_allocation_previews_are_side_effect_free_and_complete_set_is_validated(self):
        before = self.store.inventory()
        known = self.store.get('known_targets')
        steps, expected = self.editor.plan_many(self.baseline,
            [{'slot': 1, 'mode': 'ha'}, {'slot': 2, 'mode': 'ha'}], target=40001)
        self.assertEqual([s['ha_target'] for s in steps], [24575, 24574])
        self.assertEqual(self.store.inventory(), before)
        self.assertEqual(self.store.get('known_targets'), known)
        self.assertEqual(self.store.journals(), [])
        with self.assertRaises(ValueError):
            await self.save([{'slot': 1, 'mode': 'ha'}, {'slot': 2, 'mode': 'native', 'group': 292}])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])

    async def test_unowned_or_competing_protected_target_is_never_editable(self):
        self.transport.rows[0] = bytes.fromhex('01300001012408ff')
        await self.editor.read(40001)
        for mode in ('ha', 'native', 'unassigned'):
            with self.assertRaises(ValueError):
                await self.save([dict(slot=1, mode=mode, **({'group': 291} if mode == 'native' else {}))])
        self.assertFalse(self.view()['slots'][0]['editable'])
        self.assertEqual(self.view()['slots'][0]['assignment_mode'], 'unknown')
        self.assertEqual(self.transport.writes, [])
        self.transport.rows[0] = bytes.fromhex('01300001012308ff')
        await self.editor.read(40001)
        await self.save([{'slot': 1, 'mode': 'ha'}])
        self.store.observe_target(24575, 40003)
        with self.assertRaises(ValueError):
            await self.save([{'slot': 1, 'mode': 'unassigned'}])
        self.assertFalse(self.view()['slots'][0]['ha_owned'])

    async def test_allocation_skips_burned_observed_and_stale_table_targets(self):
        self.store.reserve_targets([24575])
        self.store.observe_target(24574, 40003)
        stale = replace_slot(self.baseline, 1, bytes.fromhex('013000015ffd08ff'))
        self.store.save_binding(40003, stale)
        self.store.db.execute('UPDATE bindings SET verified=0 WHERE target=40003')
        await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertEqual(self.view()['slots'][0]['target'], 24572)

    async def test_reservation_and_intent_are_durable_before_first_setter(self):
        send = self.transport.send
        checked = []
        async def inspect(frame):
            from avionmesh_pi.protocol import decrypt_mcp
            payload = decrypt_mcp(frame, self.runtime.key).payload
            if payload[0] in (2, 5):
                receipt = self.store.journals()[0]
                group = receipt['detail']['managed']['new']
                self.assertEqual(receipt['status'], 'attempted')
                self.assertIn(group, self.store.inventory()['groups'])
                self.assertIn(group['group_id'], self.store.get('known_targets'))
                self.assertEqual(owned_targets(self.store, 40001), {})
                checked.append(True)
            await send(frame)
        self.transport.send = inspect
        await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertEqual(len(checked), 3)

    async def test_failed_first_button_stops_later_button_and_never_replays_uncertain_insert(self):
        self.transport.silent = {'primary'}
        with self.assertRaises(ValueError):
            await self.save([{'slot': 2, 'mode': 'ha'}, {'slot': 3, 'mode': 'ha'}])
        journal = self.store.journals()[0]
        self.assertEqual(self.verbs(), [2])
        self.assertEqual(self.editor.saves[40001]['buttons'][1]['state'], 'not_started')
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertEqual(owned_targets(self.store, 40001), {})
        self.reconnect()
        result = await self.editor.reconcile(journal['id'])
        self.assertTrue(result['unchanged'])
        self.assertEqual(self.verbs(), [2])
        self.assertIn(24575, self.store.get('known_targets'))
        self.transport.silent = set()
        await self.save([{'slot': 2, 'mode': 'ha'}])
        self.assertEqual(self.view()['slots'][1]['target'], 24574)

    async def test_readback_recovery_activates_owned_mapping_without_resending(self):
        self.transport.fail_after_secondary = True
        with self.assertRaises(RuntimeError):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        journal = self.store.journals()[0]
        self.assertEqual(owned_targets(self.store, 40001), {})
        self.reconnect()
        with self.assertRaisesRegex(RuntimeError, 'Reconcile'):
            await self.editor.read(40001)
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertTrue((await self.editor.reconcile(journal['id']))['verified'])
        self.assertEqual(self.verbs(), [5, 2, 2])
        self.assertTrue(self.view()['slots'][0]['ha_owned'])

    async def test_delete_checkpoint_resumes_only_unattempted_ha_insertion(self):
        token = self.editor._token
        count = 0
        def fail_token():
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('injected after deletion')
            return token()
        self.editor._token = fail_token
        with self.assertRaises(RuntimeError):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        journal = self.store.journals()[0]
        self.assertEqual(journal['detail']['stages'], ['delete'])
        self.editor._token = token
        self.reconnect()
        self.assertTrue((await self.editor.resume_edit(journal['id'], allowed_models=(127,)))['verified'])
        self.assertEqual(self.verbs(), [5, 2, 2])
        self.assertEqual(self.view()['slots'][0]['assignment_mode'], 'ha')

    async def test_changed_reservation_prevents_reconcile_from_publishing_success(self):
        self.transport.fail_after_secondary = True
        with self.assertRaises(RuntimeError):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        journal = self.store.journals()[0]
        data = self.store.inventory()
        data['groups'][-1]['name'] = 'changed outside managed lifecycle'
        self.store.set('inventory', data)
        self.reconnect()
        with self.assertRaises(ValueError):
            await self.editor.reconcile(journal['id'])
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_legacy_owned_setup_can_be_managed_without_claiming_other_protected_groups(self):
        self.runtime.capabilities['wired_onboarding'] = True
        await KeypadOnboarding(self.runtime, self.editor).setup(40001)
        self.assertEqual(self.view()['slots'][0]['assignment_mode'], 'native')
        self.assertEqual([r['assignment_mode'] for r in self.view()['slots'][1:]], ['ha'] * 3)
        reserved = copy.deepcopy(self.store.inventory()['groups'])
        await self.save([{'slot': 2, 'mode': 'unassigned'}])
        self.assertEqual(self.store.inventory()['groups'], reserved)
        self.assertEqual(self.view()['slots'][1]['assignment_mode'], 'unassigned')

    async def test_readiness_is_separate_from_routing_and_requires_fresh_mapping(self):
        self.runtime.mqtt_ready = False
        self.assertTrue(self.view()['slots'][0]['ha_available'])
        await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertFalse(self.view()['slots'][0]['ha_ready'])
        self.runtime.mqtt_enabled = self.runtime.mqtt_ready = True
        data = self.store.inventory()
        device = next(d for d in data['devices'] if d['device_id'] == 40001)
        device['exposed'] = True
        self.store.set('inventory', data)
        self.assertTrue(self.view()['slots'][0]['ha_ready'])
        self.runtime.session()
        self.assertEqual(self.view()['slots'], [])
        await self.editor.read(40001)
        self.assertTrue(self.view()['slots'][0]['ha_ready'])

    async def test_ha_mode_does_not_accept_caller_target_or_battery_model(self):
        for change in ({'slot': 1, 'mode': 'ha', 'group': 24575},
                       {'slot': 1, 'mode': 'ha', 'brightness': 255}):
            with self.assertRaises(ValueError):
                await self.save([change])
        self.runtime.capabilities['inventory_complete'] = False
        with self.assertRaises(ValueError):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertEqual(self.transport.writes, [])

    async def test_owned_ha_event_mapping_is_available_only_after_verification(self):
        from avionmesh_pi.controllers import named_inputs
        from avionmesh_pi.protocol import Packet
        await self.save([{'slot': 1, 'mode': 'ha'}])
        packet = Packet(50, 40001, 0, 115, bytes.fromhex('000a5fff00ff00000000'))
        event = self.runtime.core.controllers.decode(packet, 127, 10)
        self.assertEqual(event['target'], 24575)
        self.assertEqual(event['button'], 1)
        self.assertIn(('button_1', 'activated'), named_inputs(event, 127))
        self.runtime.session()
        event = self.runtime.core.controllers.decode(packet, 127, 10)
        self.assertNotIn(('button_1', 'activated'), named_inputs(event, 127))

    async def test_background_read_yields_only_at_a_completed_read_boundary(self):
        send, frames = self.transport.send, []
        async def foreground_arrives(frame):
            frames.append(frame)
            await send(frame)
            self.runtime._foreground_requests = 1
        self.transport.send = foreground_arrives
        with self.assertRaises(BackgroundReadDeferred):
            await self.editor.read(40001, background_deadline=10010)
        self.assertEqual(len(frames), 1)
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertTrue(self.transport.ready)
        self.assertEqual(self.transport.writes, [])

    async def test_background_read_deadline_is_rechecked_after_sequence_allocation(self):
        import asyncio
        now, calls = [10], []
        self.runtime.clock = lambda: now[0]
        allocate = self.store.allocate_sequence
        def delayed_sequence():
            value = allocate()
            now[0] = 101
            return value
        self.store.allocate_sequence = delayed_sequence
        send = self.transport.send
        async def observe(frame):
            calls.append(frame)
            await send(frame)
        self.transport.send = observe
        with self.assertRaises(asyncio.TimeoutError):
            await self.editor.read(40001, background_deadline=100)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.journals(), [])
        self.assertTrue(self.transport.ready)

    async def test_background_complete_but_late_table_is_not_published(self):
        import asyncio
        now, frames = [10], []
        self.runtime.clock = lambda: now[0]
        send = self.transport.send
        async def late_final_reply(frame):
            frames.append(frame)
            await send(frame)
            if len(frames) == 10:
                now[0] = 101
        self.transport.send = late_final_reply
        with self.assertRaises(asyncio.TimeoutError):
            await self.editor.read(40001, background_deadline=100)
        self.assertEqual(len(frames), 10)
        self.assertNotIn(40001, self.runtime.core.controllers.bindings)
        self.assertNotIn(40001, self.store.bindings())
        self.assertEqual(self.transport.writes, [])
        self.assertTrue(self.transport.ready)

    async def test_group_capacity_is_checked_for_the_entire_save_before_any_write(self):
        data = self.store.inventory()
        used = {g['group_id'] for g in data['groups']}
        for target in range(1000, 1127 - len(data['groups'])):
            if target not in used:
                data['groups'].append({'group_id': target, 'name': str(target),
                    'members': [], 'exposed': False, 'protected': False})
        self.assertEqual(len(data['groups']), 127)
        self.store.set('inventory', data)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            await self.save([{'slot': 2, 'mode': 'ha'}, {'slot': 3, 'mode': 'ha'}])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])


if __name__ == '__main__':
    unittest.main()
