"""Synthetic broker recovery and hidden group reports; no broker or BLE access."""
import asyncio
import copy
import tempfile
import unittest

from avionmesh_pi.lifecycle import reload_inventory
from avionmesh_pi.mqtt import Outbox
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp, model_command
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import FakeTransport, KEY


INVENTORY = {"devices": [
    {"device_id": 40001, "product_id": 91, "name": "Controller", "exposed": True},
    {"device_id": 40002, "product_id": 134, "name": "Hidden A", "exposed": False},
    {"device_id": 40003, "product_id": 134, "name": "Hidden B", "exposed": False},
    {"device_id": 40004, "product_id": 134, "name": "Exposed", "exposed": True}],
    "groups": [{"group_id": 291, "name": "Room", "members": [40002, 40003],
                "exposed": True, "protected": False}]}


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.now = [200000]
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now[0])
        self.runtime.mesh_ready = True
        self.runtime.session()
        self.output = []
        self.runtime.sink = self.output.append
        self.runtime.mqtt_connection(True)
        self.output.clear()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def payload(self, suffix):
        return next(m['payload'] for m in reversed(self.output) if m['topic'].endswith(suffix))

    def report(self, source, brightness, sequence=1, received=None):
        self.runtime.observe(encrypt_mcp(b'\x00\x80\x73\x01\x0a\x00' + bytes([brightness]),
            KEY, sequence, source), self.now[0] if received is None else received)

    def native(self, sequence, level=128, received=None):
        payload = bytes.fromhex('008073000a012300') + bytes([level]) + bytes(4)
        self.runtime.observe(encrypt_mcp(payload, KEY, sequence, 40001),
            self.now[0] if received is None else received)

    def observed(self, source, brightness=128, kelvin=3000, when=None):
        when = self.now[0] if when is None else when
        self.runtime.local_light_states[source] = {
            'brightness': (brightness, when), 'color_temp': (kelvin, when)}

    async def test_broker_flap_preserves_waiter_session_dedup_and_verified_bindings(self):
        self.report(40002, 128)
        self.native(10)
        table = {'slots': [{'slot': 1, 'target': 291, 'flags': 8, 'level': 128}]}
        self.store.save_binding(40001, table)
        self.runtime.core.controllers.bindings[40001] = copy.deepcopy(table)
        self.runtime.core.controllers.baselines[40001] = (4, 128, self.now[0])
        self.runtime.diagnostics.start(40002)
        before = (self.runtime.generation, self.runtime.diagnostics.session,
                  self.runtime.controller_since, copy.deepcopy(self.runtime.native_observations),
                  dict(self.runtime.native_sequences), copy.deepcopy(self.runtime.local_light_states),
                  dict(self.runtime.reply_seen), dict(self.runtime.core.seen))
        boundaries = []
        self.runtime.session_observers.append(lambda: boundaries.append(True))
        pending = asyncio.create_task(self.runtime.exchange(model_command(40002, 1, 10),
            lambda p: p.crypto_source == 40002, timeout=.2))
        await asyncio.sleep(0)
        self.assertEqual(len(self.runtime.waiters), 1)
        self.now[0] += 100
        self.runtime.mqtt_connection(False)
        self.runtime.mqtt_connection(False)  # repeated failed broker connect
        self.runtime.mqtt_connection(True)
        self.assertFalse(pending.done())
        self.assertEqual(before, (self.runtime.generation, self.runtime.diagnostics.session,
                  self.runtime.controller_since, self.runtime.native_observations,
                  self.runtime.native_sequences, self.runtime.local_light_states,
                  self.runtime.reply_seen, self.runtime.core.seen))
        self.assertEqual(self.store.bindings(), {40001: table})
        self.assertEqual(self.runtime.core.controllers.bindings, {40001: table})
        self.assertIn(40001, self.runtime.core.controllers.baselines)
        self.assertTrue(self.runtime.diagnostics.active())
        self.assertEqual(boundaries, [])
        # Repeated authenticated native input remains suppressed across MQTT.
        self.output.clear()
        self.native(10)
        self.assertEqual(self.output, [])
        self.report(40002, 160, sequence=2)
        self.assertEqual((await pending).crypto_source, 40002)
        self.assertEqual(len(self.transport.frames), 1)

    async def test_real_mesh_session_still_invalidates_waiters_and_observations(self):
        self.observed(40002)
        self.native(1)
        self.store.save_binding(40001, {'slots': []})
        self.runtime.core.controllers.bindings[40001] = {'slots': []}
        pending = asyncio.create_task(self.runtime.exchange(model_command(40002, 1, 10),
            lambda p: p.crypto_source == 40002, timeout=.2))
        await asyncio.sleep(0)
        generation = self.runtime.generation
        self.runtime.mesh_ready = False
        self.runtime.session()
        with self.assertRaisesRegex(RuntimeError, 'session invalidated'):
            await pending
        self.assertEqual(self.runtime.generation, generation + 1)
        self.assertEqual(self.runtime.local_light_states, {})
        self.assertEqual(self.runtime.native_sequences, {})
        self.assertEqual(self.runtime.core.controllers.bindings, {})
        self.assertEqual(self.store.bindings(), {})
        self.assertEqual(self.payload('/291/availability'), 'offline')

    async def test_reconnect_rebuilds_retained_state_without_offline_or_cached_events(self):
        box = Outbox(self.runtime.clock)
        self.runtime.sink = box.put
        box.connection(True)
        self.native(1)
        self.assertTrue(box.events)
        box.connection(False)
        self.runtime.mqtt_connection(False)
        self.now[0] += 100
        self.report(40004, 192)
        self.report(40002, 64)
        self.native(2, 160)
        self.assertEqual(len(box.events), 0)
        self.now[0] += 100
        box.connection(True)
        self.runtime.mqtt_connection(True)
        self.assertEqual(len(box.events), 0)
        self.assertEqual(box.states[self.runtime.core.prefix + '/light/40004/state']['payload']['brightness'], 192)
        self.assertEqual(box.states[self.runtime.core.prefix + '/light/291/state']['payload']['brightness'], 64)
        self.assertFalse(any('/light/40002/' in topic for topic in box.states))
        self.assertFalse(any('/light/40002/' in m['topic'] for m in self.runtime.discovery()))
        # A receipt queued before reconnect remains local-only, even though it
        # arrives within the normal one-second transport freshness window.
        self.native(3, 180, received=self.now[0] - 1)
        self.assertEqual(len(box.events), 0)
        ready_since = self.runtime.core.ready_since
        self.now[0] += 10
        self.runtime.mqtt_connection(True)
        self.runtime.publish_current_states()  # HA birth without broker reconnect
        self.assertEqual(self.runtime.core.ready_since, ready_since)
        self.native(4, 192)
        self.assertTrue(box.events)

    async def test_reconnect_marks_stale_individuals_offline_and_clears_stale_color(self):
        self.observed(40004)
        self.runtime.local_light_states[40004]['color_temp'] = (3000, self.now[0] - 120000)
        self.runtime.publish_current_states()
        self.assertEqual(self.payload('/40004/availability'), 'online')
        self.assertIsNone(self.payload('/40004/state')['color_temp'])
        self.now[0] += 120000
        self.output.clear()
        self.runtime.mqtt_connection(False)
        self.runtime.mqtt_connection(True)
        self.assertEqual(self.payload('/40004/availability'), 'offline')
        self.assertFalse(any(m['topic'].endswith('/40004/state') for m in self.output))

    async def test_next_live_report_keeps_newer_offline_color_including_equal_millisecond(self):
        for elapsed in (0, 1):
            with self.subTest(elapsed=elapsed):
                self.runtime.core.states[40004] = {'brightness': (80, self.now[0]),
                                                   'color_temp': (2700, self.now[0])}
                self.runtime.local_light_states.pop(40004, None)
                self.runtime.mqtt_connection(False)
                self.now[0] += elapsed
                # A real accepted CT response received while MQTT is offline.
                frame = encrypt_mcp(b'\x00\x80\x73\x01\x1d\x00\x00' + (4000).to_bytes(2, 'big'),
                                    KEY, 20 + elapsed, 40004)
                self.runtime.observe(frame, self.now[0])
                self.runtime.mqtt_connection(True)
                self.assertEqual(self.payload('/40004/state')['color_temp'], 4000)
                self.report(40004, 192, sequence=30 + elapsed)
                self.assertEqual(self.payload('/40004/state')['color_temp'], 4000)
                self.assertEqual(self.payload('/40004/state')['brightness'], 192)

    async def test_duplicate_offline_light_report_cannot_replace_newer_reconnected_state(self):
        self.runtime.mqtt_connection(False)
        self.report(40004, 80, sequence=10)
        self.now[0] += 1
        self.report(40004, 192, sequence=11)
        self.runtime.mqtt_connection(True)
        self.assertEqual(self.payload('/40004/state')['brightness'], 192)
        self.output.clear()
        self.now[0] += 1
        self.report(40004, 80, sequence=10)
        self.assertEqual(self.output, [])
        self.runtime.publish_current_states()
        self.assertEqual(self.payload('/40004/state')['brightness'], 192)

    async def test_hidden_group_reports_update_immediately_without_individual_publication(self):
        self.report(40002, 0)
        state = self.payload('/291/state')
        self.assertEqual((state['state'], state['brightness'], state['observed_members']), ('OFF', 0, 1))
        self.assertIsNone(state['color_temp'])
        self.report(40003, 190)
        self.assertEqual(self.payload('/291/state')['brightness'], 190)
        self.assertEqual(self.payload('/291/state')['observed_members'], 2)
        self.assertFalse(any('/light/40002/' in m['topic'] or '/light/40003/' in m['topic'] for m in self.output))
        self.report(40003, 0, sequence=2)
        self.assertEqual(self.payload('/291/state')['state'], 'OFF')

    async def test_group_freshness_partial_agreement_and_newest_cache_fields(self):
        self.observed(40002)
        self.observed(40003, brightness=80)
        self.runtime.core.states[40002] = {'brightness': (32, self.now[0] - 1), 'color_temp': (4000, self.now[0] - 1)}
        self.runtime.publish_groups()
        self.assertEqual(self.payload('/291/state')['color_temp'], 3000)
        self.assertEqual(self.payload('/291/state')['brightness'], 128)
        self.now[0] += 1
        self.runtime.core.states[40003] = {'color_temp': (3500, self.now[0])}
        self.runtime.publish_groups()
        self.assertIsNone(self.payload('/291/state')['color_temp'])
        self.runtime.core.states.clear()
        for timestamp in (self.now[0] - 120000, self.now[0] + 1):
            with self.subTest(timestamp=timestamp):
                self.observed(40003, when=timestamp)
                self.runtime.publish_groups()
                self.assertEqual(self.payload('/291/state')['observed_members'], 1)
                self.assertIsNone(self.payload('/291/state')['color_temp'])
        self.observed(40002, when=self.now[0] - 120000)
        self.output.clear()
        self.runtime.publish_groups()
        self.assertEqual(self.payload('/291/availability'), 'offline')
        self.assertFalse(any(m['topic'].endswith('/291/state') for m in self.output))

    async def test_quarantined_member_is_missing_for_full_group_agreement(self):
        self.observed(40002)
        self.observed(40003)
        identifier = self.store.begin('remove', 40003, {})
        self.runtime.publish_groups()
        state = self.payload('/291/state')
        self.assertEqual(state['observed_members'], 1)
        self.assertIsNone(state['color_temp'])
        self.store.transition(identifier, 'prepared', 'attempted')
        self.store.transition(identifier, 'attempted', 'verified')
        self.store.begin('group_membership', 40002, {'group': 291})
        self.output.clear()
        self.runtime.publish_groups()
        self.assertEqual(self.payload('/291/availability'), 'offline')
        self.assertFalse(any(m['topic'].endswith('/291/state') for m in self.output))

    async def test_metadata_rename_preserves_hidden_member_timestamps_and_group_snapshot(self):
        self.observed(40002)
        self.observed(40003)
        before = copy.deepcopy(self.runtime.local_light_states)
        self.now[0] += 100
        data = self.store.inventory()
        data['groups'][0]['name'] = 'Renamed room'
        data['devices'][1]['name'] = 'Renamed hidden member'
        self.store.import_inventory(data)
        self.output.clear()
        reload_inventory(self.runtime)
        self.assertEqual(self.runtime.local_light_states, before)
        self.assertEqual(self.payload('/291/availability'), 'online')
        self.assertEqual(self.payload('/291/state')['color_temp'], 3000)
        self.assertEqual(self.payload('/pi_lab_group_291/config')['name'], 'Renamed room')
        self.assertEqual(self.transport.frames, [])

    async def test_newly_exposed_member_needs_fresh_read_after_metadata_reload(self):
        self.observed(40002)
        self.observed(40003)
        data = self.store.inventory()
        data['devices'][1]['exposed'] = True
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        self.assertNotIn(40002, self.runtime.local_light_states)
        self.output.clear()
        self.runtime.mqtt_connection(False)
        self.runtime.mqtt_connection(True)
        self.assertEqual(self.payload('/40002/availability'), 'offline')
        self.assertFalse(any(m['topic'].endswith('/40002/state') for m in self.output))
        self.assertEqual(self.payload('/291/state')['observed_members'], 1)
        self.report(40002, 180)
        self.assertEqual(self.payload('/40002/availability'), 'online')

    async def test_discovery_after_reconnect_keeps_retained_available_snapshots(self):
        self.observed(40002)
        self.observed(40003)
        self.observed(40004)
        box = Outbox(self.runtime.clock)
        self.runtime.sink = box.put
        self.runtime.mqtt_connection(False)
        box.connection(True)
        self.runtime.mqtt_connection(True)
        self.runtime.emit(self.runtime.discovery())
        for target in (291, 40004):
            self.assertEqual(box.states[f'{self.runtime.core.prefix}/light/{target}/availability']['payload'], 'online')
            self.assertEqual(box.states[f'{self.runtime.core.prefix}/light/{target}/state']['payload']['color_temp'], 3000)
        self.assertFalse(box.events)

    async def test_mesh_offline_hidden_and_invalid_groups_publish_no_state(self):
        self.observed(40002)
        self.observed(40003)
        self.runtime.mesh_ready = False
        self.runtime.publish_current_states()
        self.assertEqual(self.payload('/291/availability'), 'offline')
        self.assertFalse(any(m['topic'].endswith('/291/state') for m in self.output))
        self.runtime.mesh_ready = True
        data = self.store.inventory()
        data['groups'][0]['members'].append(40001)
        self.store.import_inventory(data)
        self.output.clear()
        self.runtime.publish_groups()
        self.assertEqual(self.payload('/291/availability'), 'offline')
        self.assertFalse(any(m['topic'].endswith('/291/state') for m in self.output))
        data['groups'][0]['exposed'] = False
        self.store.import_inventory(data)
        self.output.clear()
        self.runtime.publish_groups()
        self.assertEqual(self.output, [])

    async def test_first_setter_admission_after_sequence_allocation_prevents_stale_dispatch(self):
        allocate = self.store.allocate_sequence
        def delayed_allocate():
            sequence = allocate()
            self.now[0] += 5001
            return sequence
        self.store.allocate_sequence = delayed_allocate
        accepted_at = self.now[0]
        def admission():
            if self.now[0] - accepted_at > 5000:
                raise RuntimeError('Synthetic stale command')
        with self.assertRaisesRegex(RuntimeError, 'stale command'):
            await self.runtime.control(40004, {'brightness': 128}, confirm=False, admission=admission)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.get('sequence'), 1)

    async def test_accepted_compound_control_finishes_once_after_broker_loss(self):
        checks = []
        def admission():
            self.assertTrue(self.runtime.mqtt_ready)
            checks.append(True)
        send = self.transport.send
        async def lose_broker(frame):
            await send(frame)
            self.runtime.mqtt_connection(False)
            self.now[0] += 5001
        self.transport.send = lose_broker
        result = await self.runtime.control(40004, {'brightness': 128, 'color_temp': 3000},
            confirm=False, admission=admission)
        self.assertTrue(result['sent'])
        self.assertEqual(checks, [True])
        packets = [decrypt_mcp(frame, KEY) for frame in self.transport.frames]
        self.assertEqual([p.payload[0] for p in packets], [0, 0, 1, 1])

