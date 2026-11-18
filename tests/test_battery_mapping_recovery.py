"""Synthetic cached battery HA routing; never physical assignment qualification."""
import copy
import unittest
from unittest.mock import AsyncMock, patch

import test_battery_ha_edit as battery_case
from avionmesh_pi.battery_mapping import CACHE_KEY
from avionmesh_pi.controller_workflow import controller_workflows
from avionmesh_pi.controllers import binding_status
from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.protocol import encrypt_mcp, plan_light_command
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_keypad_lost_reply import LossyTransport
from test_service import KEY


class BatteryMappingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = battery_case.BatteryManagedEditTests.asyncSetUp
    asyncTearDown = battery_case.BatteryManagedEditTests.asyncTearDown
    sleep = battery_case.BatteryManagedEditTests.sleep
    save = battery_case.BatteryManagedEditTests.save
    reconnect = battery_case.BatteryManagedEditTests.reconnect

    async def prepared(self):
        await self.save([{'slot': 1, 'mode': 'ha'}])
        self.table = copy.deepcopy(self.runtime.core.controllers.bindings[40001])
        self.target = self.table['slots'][0]['target']
        self.messages = []
        self.runtime.sink = self.messages.append
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'cached')

    def restart(self):
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.transport = LossyTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.capabilities = {'battery_editor': True, 'inventory_complete': True}
        self.editor = KeypadService(self.runtime)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.mqtt_enabled = True
        self.now += 100
        self.runtime.session()
        self.messages = []
        self.runtime.sink = self.messages.append
        self.transport.send = AsyncMock(side_effect=AssertionError('Recovery must not send radio frames'))

    def press(self, sequence=10000, *, target=None, level=255, source=40001, received=None):
        frame = encrypt_mcp(plan_light_command(self.target if target is None else target, brightness=level),
                            KEY, sequence, source)
        self.runtime.observe(frame, self.now if received is None else received)
        return frame

    def activations(self):
        return [m['payload'] for m in self.messages if '/input/button_' in m['topic']
                and not m['retain']]

    async def test_first_press_after_process_restart_routes_from_owned_cache_without_radio(self):
        await self.prepared()
        self.restart()
        self.assertEqual(self.store.bindings(), {})
        self.assertEqual(self.runtime.core.controllers.bindings, {})
        self.press()
        self.assertEqual(len(self.activations()), 1)
        self.assertEqual(self.activations()[0]['button'], 1)
        self.assertEqual(self.activations()[0]['button_mapping'], 'cached_table')
        self.transport.send.assert_not_awaited()
        self.assertEqual(self.runtime.core.controllers.bindings, {})

    async def test_mesh_and_broker_boundaries_keep_cache_but_never_replay_events(self):
        await self.prepared()
        self.reconnect()
        self.press()
        self.assertEqual(self.activations()[0]['button_mapping'], 'cached_table')
        self.runtime.mqtt_connection(False)
        self.runtime.mqtt_connection(True)
        self.assertEqual(len(self.activations()), 1)
        self.now += 100
        self.reconnect()
        self.press(10001)
        self.assertEqual(len(self.activations()), 2)

    async def test_cached_status_never_unlocks_editor_or_claims_fresh_readback(self):
        await self.prepared()
        self.restart()
        self.press()
        view = controller_workflows(self.runtime)[0]
        self.assertEqual(view['assignment_evidence'], 'cached_table')
        self.assertFalse(view['assignment_read_back'])
        self.assertIsNone(view['revision'])
        self.assertEqual(view['slots'], [])
        self.assertFalse(view['actions']['edit_assignment']['enabled'])
        self.assertEqual(view['cached_mapping']['slots'][0]['slot'], 1)
        self.assertEqual(view['last_event']['button_mapping'], 'cached_table')
        mapping = self.runtime.core.controllers.mapping_for_events(40001)
        self.assertEqual(binding_status(mapping, 1)[0], 'cached')
        self.assertEqual(binding_status(mapping, 2)[0], 'needs_refresh')

    async def test_retained_discovery_exposes_cached_evidence_only_for_owned_slots(self):
        await self.prepared()
        self.restart()
        publications = {m['topic']: m['payload'] for m in self.runtime.discovery()}
        base = self.runtime.core.prefix + '/controller/40001/input/'
        self.assertEqual(publications[base + 'button_1/availability'], 'online')
        mapping = publications[base + 'button_1/mapping']
        self.assertEqual(mapping['mapping_status'], 'cached')
        self.assertEqual(mapping['mapping_evidence'], 'cached_table')
        self.assertFalse(mapping['assignment_read_back'])
        self.assertEqual(mapping['target_id'], self.target)
        for slot in (2, 3, 4):
            self.assertEqual(publications[base + f'button_{slot}/availability'], 'offline')
            self.assertEqual(publications[f'homeassistant/device_automation/{self.runtime.core.node}_40001_button_{slot}/config'], '')
        self.transport.send.assert_not_awaited()

    async def test_legacy_unverified_binding_without_new_cache_is_not_restored(self):
        await self.prepared()
        self.store.set(CACHE_KEY, {})
        self.restart()
        self.press()
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'unavailable')

    async def test_failed_explicit_read_revokes_cache_durably(self):
        await self.prepared()
        with patch.object(self.editor, 'verified_rounds', new=AsyncMock(side_effect=RuntimeError('synthetic sleep'))):
            with self.assertRaisesRegex(RuntimeError, 'synthetic sleep'):
                await self.editor.read(40001)
        self.restart()
        self.press()
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_pending_write_cannot_restore_even_after_receipt_is_cancelled(self):
        await self.prepared()
        identifier = self.store.begin('keypad_edit', 40001, {'product_id': 126})
        self.restart()
        self.press()
        self.assertEqual(self.activations(), [])
        self.store.transition(identifier, 'prepared', 'cancelled')
        self.reconnect()
        self.press(10001)
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_sharing_change_revokes_and_restoring_metadata_does_not_resurrect(self):
        await self.prepared()
        inventory = self.store.inventory()
        changed = copy.deepcopy(inventory)
        changed['devices'][0]['exposed'] = False
        self.store.import_inventory(changed)
        self.reconnect()
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
        self.store.import_inventory(inventory)
        self.reconnect()
        self.press()
        self.assertEqual(self.activations(), [])

    async def test_removal_reset_and_retirement_prevent_reuse(self):
        for change in ('remove_binding', 'retired_address', 'verified_reset'):
            with self.subTest(change=change):
                await self.prepared()
                if change == 'remove_binding':
                    self.store.db.execute('DELETE FROM bindings WHERE target=40001')
                elif change == 'retired_address':
                    self.store.set('retired_addresses', [40001])
                else:
                    identifier = self.store.begin('external_reset', 40001, {})
                    self.store.transition(identifier, 'prepared', 'attempted')
                    self.store.transition(identifier, 'attempted', 'verified')
                self.reconnect()
                self.press()
                self.assertEqual(self.activations(), [])
                # Restore only for the next independent synthetic case. Re-read
                # creates new provenance; restoring metadata alone cannot do so.
                self.store.set('retired_addresses', [])
                self.transport.rows = [bytes.fromhex(row['raw']) for row in self.table['slots']]
                await self.editor.read(40001)

    async def test_unknown_target_revokes_before_first_numbered_event(self):
        await self.prepared()
        self.restart()
        self.press(target=300)
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.editor.battery_cache.status(40001)['reason'], 'observed_target_not_in_cached_table')
        self.press(10001)
        self.assertEqual(self.activations(), [])
        self.restart()
        self.press()
        self.assertEqual(self.activations(), [])

    async def test_contradictory_table_report_revokes_but_stale_report_cannot(self):
        await self.prepared()
        self.restart()
        self.press(10000)
        wrong = bytes.fromhex('01300001012c08ff')
        def report(sequence):
            self.runtime.observe(encrypt_mcp(b'\0\x80\x73' + wrong, KEY, sequence, 40001), self.now)
        report(9999)
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'cached')
        report(10001)
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')
        self.press(10002)
        self.assertEqual(len(self.activations()), 1)

    async def test_duplicates_old_sequences_and_previous_session_frames_do_not_activate(self):
        await self.prepared()
        self.restart()
        frame = self.press(10000)
        self.runtime.observe(frame, self.now)
        self.press(9999)
        self.press(10001, received=self.now - 1)
        self.assertEqual(len(self.activations()), 1)
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'cached')

    async def test_competing_private_target_owner_revokes_cache(self):
        await self.prepared()
        self.restart()
        self.store.observe_target(self.target, 40002)
        self.press()
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_native_and_unowned_slots_are_not_recovered_as_numbered_ha_events(self):
        await self.prepared()
        self.transport.send = lambda frame: LossyTransport.send(self.transport, frame)
        await self.save([{'slot': 2, 'mode': 'native', 'group': 293}])
        self.restart()
        self.press(target=293)
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'cached')
        self.press(10001)
        self.assertEqual(len(self.activations()), 1)

    async def test_no_background_battery_queries_and_preview_never_restores(self):
        from avionmesh_pi.keypad_refresh import WiredKeypadRefresh
        await self.prepared()
        self.restart()
        refresher = WiredKeypadRefresh(self.runtime, self.editor, settle=0)
        for _ in range(4):
            self.now += 30000
            await refresher.tick()
        self.transport.send.assert_not_awaited()
        records = self.store.get(CACHE_KEY)
        self.runtime.preview_only = True
        self.assertIsNone(self.runtime.core.controllers.mapping_for_events(40001))
        self.assertIsNone(controller_workflows(self.runtime, read_only=True)[0]['cached_mapping'])
        self.assertEqual(self.store.get(CACHE_KEY), records)

    async def test_out_of_band_command_revokes_even_before_reconnect(self):
        await self.prepared()
        self.press(target=300)
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.runtime.core.controllers.bindings, {})
        self.assertEqual(self.store.bindings(), {})
        self.restart()
        self.press()
        self.assertEqual(self.activations(), [])

    async def test_corrupt_cached_table_fails_closed_without_promoting_stale_rows(self):
        await self.prepared()
        records = self.store.get(CACHE_KEY)
        records['40001']['table']['slots'][0]['target'] = 300
        self.store.set(CACHE_KEY, records)
        self.restart()
        self.press()
        self.assertEqual(self.activations(), [])
        self.assertEqual(self.editor.battery_cache.status(40001)['status'], 'revoked')

    async def test_router_replacement_reattaches_cache_and_new_read_replaces_revoked_evidence(self):
        from avionmesh_pi.bridge import Bridge, load_devices
        await self.prepared()
        cache = self.editor.battery_cache
        self.runtime.core = Bridge(load_devices(self.store.inventory()['devices']), KEY)
        self.reconnect()
        self.assertIs(self.runtime.core.controllers.battery_cache, cache)
        self.press()
        self.assertEqual(self.activations()[0]['button_mapping'], 'cached_table')
        data = self.store.inventory()
        data['devices'][0]['exposed'] = False
        self.store.import_inventory(data)
        self.runtime.core = Bridge(load_devices(data['devices']), KEY)
        self.reconnect()
        self.assertEqual(cache.status(40001)['status'], 'revoked')
        data['devices'][0]['exposed'] = True
        self.store.import_inventory(data)
        self.runtime.core = Bridge(load_devices(data['devices']), KEY)
        self.reconnect()
        self.assertEqual(cache.status(40001)['status'], 'revoked')
        await self.editor.read(40001)
        self.assertIs(self.runtime.core.controllers.battery_cache, cache)
        self.assertEqual(cache.status(40001)['status'], 'cached')
        self.reconnect()
        self.messages.clear()
        self.press(10001)
        self.assertEqual(len(self.activations()), 1)
        another_service = KeypadService(self.runtime)
        self.assertIs(another_service.battery_cache, cache)
        self.assertEqual(self.runtime.packet_observers.count(cache.observe), 1)
        self.assertEqual(self.runtime.session_observers.count(cache.attach_current_router), 1)

    async def test_cache_contains_no_raw_mesh_key_and_remains_bounded_to_inventory(self):
        await self.prepared()
        records = self.store.get(CACHE_KEY)
        self.assertEqual(list(records), ['40001'])
        self.assertNotIn(KEY.hex(), str(records))
        self.assertNotIn('mesh', records['40001'])
        self.assertEqual(records['40001']['context']['slots'], [1])
