"""Group feedback scheduling with synthetic state and no network or hardware."""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.mqtt import MqttService
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import GROUP_COMMAND_SETTLE_MS, Runtime
from support import synthetic_store as Store


KEY = bytes(range(16))


class Mesh:
    ready, dropped = True, 0

    def __init__(self, owner):
        self.owner, self.sequence = owner, 100
        self.frames = []
        self.level = 0
        self.kelvin = 3000
        self.silent = False
        self.on_send = None

    async def send(self, frame):
        packet = decrypt_mcp(frame, KEY)
        self.frames.append((self.owner.now, packet))
        if self.on_send is not None:
            await self.on_send(packet)
        if packet.payload[0] == 0:
            if packet.payload[1] == 10:
                self.level = packet.payload[5]
            else:
                self.kelvin = int.from_bytes(packet.payload[6:8], 'big')
        elif not self.silent:
            self.reply(packet.mcp_address, packet.payload[1])

    def reply(self, source, noun):
        self.sequence += 1
        value = bytes([self.level]) if noun == 10 else b'\x01' + self.kelvin.to_bytes(2, 'big')
        payload = b'\x00\x80\x73' + bytes([1, noun, 0]) + value
        self.owner.runtime.observe(encrypt_mcp(payload, KEY, self.sequence, source), self.owner.now)


class GroupFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.inventory = {'devices': [
            {'device_id': 40001 + n, 'product_id': 93, 'name': 'Light %d' % n, 'exposed': True}
            for n in range(10)] + [
            {'device_id': 40100, 'product_id': 91, 'name': 'Dimmer', 'exposed': False},
            {'device_id': 40101, 'product_id': 127, 'name': 'Keypad', 'exposed': False}], 'groups': [
            {'group_id': 258, 'name': 'Theatre', 'members': list(range(40001, 40011)),
             'exposed': True, 'protected': False},
            {'group_id': 259, 'name': 'Subset', 'members': [40001, 40002],
             'exposed': True, 'protected': False}]}
        self.store.import_inventory(self.inventory)
        self.now = 1000
        self.mesh = Mesh(self)
        self.runtime = Runtime(self.store, KEY, self.mesh, clock=lambda: self.now)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        self.messages = []
        self.runtime.sink = self.messages.append
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(self.runtime.waiters, [])
        self.assertFalse(self.runtime.operation.locked())
        self.assertEqual(self.runtime._foreground_requests, 0)
        self.assertIsNone(self.runtime._background_read_task)
        self.store.close()
        self.temp.cleanup()

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('Expected async boundary was not reached')

    async def command(self, state='ON', target=258):
        async with self.runtime.foreground_request():
            # Unit-level scheduling tests stand in for an admitted worker. Tests
            # of worker handoff below run the actual observation task.
            with patch.object(self.runtime, '_observation_task', self.runtime._observation_task or self):
                return await self.runtime.control(target, {'state': state}, confirm=False)

    def setters(self):
        return [(when, p) for when, p in self.mesh.frames if p.payload[0] == 0]

    def reads(self):
        return [(when, p) for when, p in self.mesh.frames if p.payload[0] == 1]

    async def worker(self, stop, *, on_sleep=None, settle=1000, tick_seconds=.1, **options):
        """Virtual time, with production scheduling and exchange paths intact."""
        original_sleep = asyncio.sleep
        ticks = 0

        async def sleep(delay):
            self.now += int(delay * 1000)
            if on_sleep is not None:
                await on_sleep(delay)
            await original_sleep(0)

        async def tick(delay):
            nonlocal ticks
            ticks += 1
            if ticks > 200:
                self.fail('Worker did not reach expected boundary')
            if stop():
                raise asyncio.CancelledError()
            await sleep(delay)

        with patch('avionmesh_pi.runtime.asyncio.sleep', side_effect=sleep), \
                patch.object(self.runtime, '_wait_observation_refresh', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime.observation_refresh(settle=settle, tick=tick_seconds, **options)
        self.assertIsNone(self.runtime._observation_task)

    async def test_ten_member_group_sends_one_setter_without_inline_reads_or_optimistic_state(self):
        result = await self.command()
        self.assertEqual(result, {'sent': True, 'readbacks': [], 'confirmed': False, 'feedback_pending': True, 'feedback_after_ms': self.now})
        self.assertEqual(len(self.mesh.frames), 1)
        packet = self.mesh.frames[0][1]
        self.assertEqual((packet.mcp_address, packet.payload.hex()), (0, '000a010200ff00000000'))
        self.assertEqual(self.runtime.device_states(), {})
        self.assertFalse(any(m['topic'].endswith('/state') for m in self.messages))
        self.assertEqual(len(self.runtime._group_refresh), 10)
        self.assertFalse(self.runtime.operation.locked())
        self.assertIsNone(self.runtime._observation_task)

    async def test_targeted_worker_settles_and_paces_brightness_without_color_reads(self):
        await self.command()
        sent = self.now
        await self.worker(lambda: not self.runtime._group_refresh)
        reads = self.reads()
        self.assertEqual(len(reads), 10)
        self.assertEqual(reads[0][0] - sent, GROUP_COMMAND_SETTLE_MS)
        self.assertTrue(all(p.payload[1] == 10 for _, p in reads))
        self.assertTrue(all(b[0] - a[0] == 150 for a, b in zip(reads, reads[1:])))
        self.assertEqual(reads[-1][0] - sent, 1500)
        self.assertEqual(len(self.setters()), 1)
        self.assertEqual(len(self.runtime.device_states()), 10)

    async def test_group_off_remains_observed_until_last_previously_on_member_answers(self):
        self.mesh.level = 255
        for member in range(40001, 40011):
            self.mesh.reply(member, 10)
        self.messages.clear()
        await self.command('OFF')
        self.assertEqual(self.messages, [])
        sent = self.now
        await self.worker(lambda: not self.runtime._group_refresh, tick_seconds=1)
        states = [m['payload'] for m in self.messages
                  if m['topic'].endswith('/light/258/state')]
        self.assertEqual([s['state'] for s in states], ['ON'] * 9 + ['OFF'])
        self.assertEqual([s['brightness'] for s in states], [255] * 9 + [0])
        self.assertEqual(self.reads()[-1][0] - sent, 1500)

    async def test_production_tick_reads_at_150ms_not_1000ms(self):
        await self.command(target=259)
        sent = self.now
        await self.worker(lambda: not self.runtime._group_refresh, tick_seconds=1)
        self.assertEqual([when - sent for when, _ in self.reads()], [150, 300])

    async def test_targeted_feedback_precedes_due_periodic_reads_and_controller_probes(self):
        self.runtime.core.controllers.local_modes = {40100}
        # Subset at the END of inventory: sorting all lights would query others first.
        self.inventory['groups'][1]['members'] = [40009, 40010]
        self.store.import_inventory(self.inventory)
        await self.command(target=259)
        self.now += 6000  # controller probe and periodic sweep are also due
        async def probe_after_feedback(*args, **kwargs):
            self.assertFalse(self.runtime._group_refresh)
            self.assertEqual(len(self.reads()), 2)
        with patch.object(self.runtime, 'read_controller_baseline', side_effect=probe_after_feedback) as probe:
            await self.worker(lambda: not self.runtime._group_refresh, settle=0)
        self.assertEqual([p.mcp_address for _, p in self.reads()], [40009, 40010])
        self.assertTrue(all(p.payload[1] == 10 for _, p in self.reads()))
        # No probe was allowed ahead of either pending member.
        self.assertLessEqual(probe.await_count, 1)

    async def test_new_native_revision_during_last_read_blocks_unrelated_refreshers(self):
        self.mesh.level = 255
        self.native(target=259)
        self.runtime.core.controllers.local_modes = {40100}
        sent = False
        async def during_read(packet):
            nonlocal sent
            if packet.payload[0] == 1 and packet.mcp_address == 40002 and not sent:
                sent = True
                self.mesh.level = 128
                self.native(11, target=259, level=128)
        self.mesh.on_send = during_read
        class Refresher:
            async def tick(inner):
                self.assertFalse(self.runtime._group_refresh)
        self.runtime.keypad_binding_refresher = Refresher()
        async def probe_after_feedback(*args, **kwargs):
            self.assertFalse(self.runtime._group_refresh)
        self.now += 6000
        with patch.object(self.runtime, 'read_controller_baseline', side_effect=probe_after_feedback):
            await self.worker(lambda: not self.runtime._group_refresh)
        self.assertTrue(sent)
        self.assertEqual([p.mcp_address for _, p in self.reads()], [40001, 40002, 40001, 40002])

    async def test_periodic_color_refresh_still_runs_after_targeted_brightness(self):
        await self.command(target=259)
        await self.worker(lambda: self.runtime.observation_refresh_progress['passes'] >= 2, settle=0)
        reads = self.reads()
        self.assertEqual([p.payload[1] for _, p in reads[:2]], [10, 10])
        self.assertEqual(len(reads), 22)
        self.assertEqual(sum(p.payload[1] == 29 for _, p in reads), 10)

    async def test_periodic_pass_preempts_unrelated_next_member_for_new_feedback(self):
        self.inventory['groups'][1]['members'] = [40009, 40010]
        self.store.import_inventory(self.inventory)
        sent = None
        async def during_gap(delay):
            nonlocal sent
            if delay == .5 and sent is None:
                sent = self.now
                await self.command(target=259)
        await self.worker(lambda: sent is not None and not self.runtime._group_refresh,
                          settle=0, tick_seconds=1, on_sleep=during_gap)
        self.assertEqual([p.mcp_address for _, p in self.reads()], [40001, 40001, 40009, 40010])
        self.assertEqual(self.reads()[2][0] - sent, 150)

    def native(self, sequence=10, *, source=40100, target=258, level=255, delta=None,
               address=0, received=None):
        payload = bytes([17 if delta is not None else 0, 10]) + target.to_bytes(2, 'big')
        payload += bytes([0, delta if delta is not None else level, 0, 0, 0, 0])
        frame = encrypt_mcp(address.to_bytes(2, 'little') + b'\x73' + payload, KEY, sequence, source)
        with patch.object(self.runtime, '_observation_task', self.runtime._observation_task or self):
            self.runtime.observe(frame, self.now if received is None else received)
        return frame

    async def test_native_controller_refreshes_observations_without_optimistic_state_or_setters(self):
        self.native()
        self.assertEqual(self.runtime.device_states(), {})
        self.assertFalse(any(m['topic'].endswith('/state') for m in self.messages))
        self.mesh.level = 255  # independently observed physical result in fake mesh
        sent = self.now
        await self.worker(lambda: not self.runtime._group_refresh, tick_seconds=1)
        self.assertEqual(len(self.reads()), 10)
        self.assertEqual(self.reads()[0][0] - sent, 150)
        self.assertEqual(self.setters(), [])
        self.assertEqual(self.runtime.device_states()['40001']['brightness']['value'], 255)

    async def test_native_hold_changes_and_duplicate_packets_cannot_postpone_reads(self):
        self.native(source=40101, delta=13)
        due = self.runtime._group_refresh_due
        for sequence in range(11, 21):
            self.now += 100
            self.native(sequence, source=40101, delta=13)
            self.assertEqual(self.runtime._group_refresh_due, due)
        revision = dict(self.runtime._group_refresh)
        self.native(20, source=40101, delta=13)
        self.native(19, source=40101, delta=243)
        self.assertEqual(self.runtime._group_refresh, revision)
        self.assertEqual(self.runtime._group_refresh_due, due)
        self.mesh.level = 128
        self.native(21, source=40101, level=128)
        self.assertEqual(self.runtime._group_refresh_due, due)
        await self.worker(lambda: not self.runtime._group_refresh)
        self.assertEqual(len(self.reads()), 10)
        self.assertEqual(self.setters(), [])
        self.runtime.session()
        self.assertEqual(self.runtime._feedback_sequences, {})
        self.assertEqual(self.runtime._group_refresh, {})

    async def test_different_native_group_gets_its_own_full_settle_window(self):
        self.mesh.level = 255
        self.native(target=258)
        self.now += 400
        self.native(11, target=259)
        sent = self.now
        self.assertEqual(self.runtime._group_refresh_due, sent + 150)
        await self.worker(lambda: not self.runtime._group_refresh, tick_seconds=1)
        self.assertEqual(self.reads()[0][0] - sent, 150)
        self.assertEqual(len(self.reads()), 10)
        self.assertEqual(self.setters(), [])

    async def test_native_private_unknown_stale_pending_and_malformed_targets_are_ignored(self):
        self.inventory['groups'].append({'group_id': 260, 'name': 'Private',
            'members': [], 'protected': True, 'exposed': False})
        self.store.import_inventory(self.inventory)
        for sequence, options in enumerate(({'target': 260}, {'target': 999}, {'source': 40999},
                {'received': self.now - 1001}, {'target': 258, 'address': 40001}), 1):
            self.native(sequence, **options)
            self.assertEqual(self.runtime._group_refresh, {})
        self.store.begin('group_membership', 40001, {'group': 258})
        self.native(20)
        self.assertEqual(self.runtime._group_refresh, {})
        self.store.begin('claim', 40002, {'uuid': '00' * 16})
        self.native(21, target=0, address=40002)
        self.assertEqual(self.runtime._group_refresh, {})
        self.store.begin('dimmer_edit', 40100, {})
        self.native(22, target=0, address=40003)
        self.assertEqual(self.runtime._group_refresh, {})

    async def test_documented_native_direct_light_command_refreshes_only_that_light(self):
        self.mesh.level = 255
        self.native(target=0, address=40003)
        await self.worker(lambda: not self.runtime._group_refresh)
        self.assertEqual([p.mcp_address for _, p in self.reads()], [40003])
        self.assertEqual(self.setters(), [])

    async def test_delayed_first_member_does_not_block_other_observed_states(self):
        self.mesh.level = 255
        for member in range(40001, 40011):
            self.mesh.reply(member, 10)
        await self.command('OFF')
        self.now += GROUP_COMMAND_SETTLE_MS
        self.mesh.silent = True
        last_requested = asyncio.Event()
        async def answer_other_members(packet):
            if packet.payload[0] == 1 and packet.mcp_address != 40001:
                self.mesh.reply(packet.mcp_address, packet.payload[1])
                if packet.mcp_address == 40010:
                    last_requested.set()
        self.mesh.on_send = answer_other_members
        started = asyncio.get_running_loop().time()
        task = self.task(self.runtime.observation_refresh(settle=1000))
        # A sequential exchange would still be waiting three seconds on 40001.
        await asyncio.wait_for(last_requested.wait(), 2.2)
        self.assertLess(asyncio.get_running_loop().time() - started, 2.2)
        self.assertEqual(len(self.reads()), 10)
        self.assertEqual(self.runtime.waiters, [])
        self.assertTrue(all(self.runtime.device_states()[str(i)]['brightness']['value'] == 0
                            for i in range(40002, 40011)))
        states = [m['payload'] for m in self.messages if m['topic'].endswith('/light/258/state')]
        self.assertEqual(states[-1]['state'], 'ON')  # missing member remains honestly ON
        self.mesh.reply(40001, 10)
        states = [m['payload'] for m in self.messages if m['topic'].endswith('/light/258/state')]
        self.assertEqual(states[-1]['state'], 'OFF')
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_early_mismatch_gets_one_later_read_without_replaying_setter(self):
        await self.command(target=259)
        self.mesh.level = 0  # physical fixture has not finished its transition yet
        start = self.now
        async def finish_transition(delay):
            if self.now - start >= 600:
                self.mesh.level = 255
        await self.worker(lambda: not self.runtime._group_refresh and not self.runtime._feedback_checks,
                          tick_seconds=1, on_sleep=finish_transition)
        self.assertEqual(len(self.reads()), 4)
        self.assertEqual(len(self.setters()), 1)
        self.assertGreaterEqual(self.reads()[2][0] - self.reads()[0][0], 600)
        self.assertEqual(self.runtime.device_states()['40001']['brightness']['value'], 255)

    async def test_absent_reports_get_at_most_two_reads_per_member(self):
        await self.command()
        self.mesh.silent = True
        await self.worker(lambda: not self.runtime._group_refresh and not self.runtime._feedback_checks,
                          tick_seconds=1)
        self.assertEqual(len(self.reads()), 20)
        self.assertEqual(len(self.setters()), 1)
        self.assertEqual(self.runtime.device_states(), {})
        self.now += 10000
        self.runtime._recheck_feedback()
        self.assertEqual(self.runtime._group_refresh, {})

    async def test_new_command_supersedes_pending_recheck_and_session_discards_it(self):
        await self.command(target=259)
        self.mesh.silent = True
        await self.worker(lambda: not self.runtime._group_refresh)
        old_revision = self.runtime._group_refresh_revision
        await self.command('OFF', target=259)
        self.assertTrue(all(c['revision'] > old_revision and c['expected'] == 0
                            for c in self.runtime._feedback_checks.values()))
        self.runtime.session()
        self.now += 10000
        self.runtime._recheck_feedback()
        self.assertEqual(self.runtime._group_refresh, {})
        self.assertEqual(self.runtime._feedback_checks, {})
        self.assertEqual(self.runtime._feedback_fields, {})

    async def test_same_command_key_replaces_old_recheck_after_first_pass(self):
        await self.command(target=259)
        self.mesh.silent = True
        await self.worker(lambda: not self.runtime._group_refresh)
        previous = self.runtime._group_refresh_revision
        self.now += 100
        await self.command(target=259)
        self.assertTrue(all(c['revision'] > previous and c['since'] is None
                            for c in self.runtime._feedback_checks.values()))
        self.now += 600
        self.runtime._recheck_feedback()
        self.mesh.silent = False
        await self.worker(lambda: not self.runtime._group_refresh and not self.runtime._feedback_checks)
        self.assertEqual(len(self.reads()), 4)
        self.assertEqual(len(self.setters()), 2)

    async def test_session_change_during_queued_query_prevents_followup_field(self):
        with patch.object(self.runtime, '_observation_task', self):
            await self.runtime.control(40001, {'brightness': 128, 'color_temp': 4000}, confirm=False)
        async def boundary(packet):
            if packet.payload[0] == 1:
                self.runtime.session()
        self.mesh.on_send = boundary
        await self.worker(lambda: not self.runtime._group_refresh and not self.runtime._feedback_checks)
        self.assertEqual([p.payload[1] for _, p in self.reads()], [10])
        self.assertEqual(len(self.setters()), 2)
        self.assertEqual(self.runtime._feedback_fields, {})

    async def test_compound_feedback_preserves_unsent_color_when_foreground_preempts(self):
        with patch.object(self.runtime, '_observation_task', self):
            await self.runtime.control(40001, {'brightness': 128, 'color_temp': 4000}, confirm=False)
        paused = False
        async def reserve_after_brightness(packet):
            nonlocal paused
            if packet.payload[0] == 1 and packet.payload[1] == 10 and not paused:
                paused = True
                self.runtime._foreground_requests += 1
        self.mesh.on_send = reserve_after_brightness
        async def release_later(delay):
            if paused and self.runtime._foreground_requests and len(self.reads()) == 1:
                # Keep the reservation through the between-field check, then
                # release it at the member pacing boundary.
                if self.runtime._background_read_task is None:
                    self.runtime._foreground_requests -= 1
        await self.worker(lambda: not self.runtime._group_refresh and not self.runtime._feedback_checks,
                          on_sleep=release_later)
        self.assertEqual([p.payload[1] for _, p in self.reads()], [10, 29])
        self.assertEqual(len(self.setters()), 2)

    async def test_repeated_overlapping_groups_coalesce_members_and_reset_settling(self):
        await self.command()
        self.now += 400
        await self.command('OFF', 259)
        self.assertEqual(len(self.runtime._group_refresh), 10)
        latest = self.now
        await self.worker(lambda: not self.runtime._group_refresh)
        # This fixture applies the subset OFF to every simulated light. The eight
        # unexpected OFF readings get one bounded read, never another setter.
        self.assertEqual(len(self.reads()), 18)
        self.assertGreaterEqual(self.reads()[0][0] - latest, GROUP_COMMAND_SETTLE_MS)
        self.assertEqual([p.payload[5] for _, p in self.setters()], [255, 0])

    async def test_subset_refresh_does_not_poll_unrelated_lights(self):
        await self.command(target=259)
        await self.worker(lambda: not self.runtime._group_refresh)
        self.assertEqual({p.mcp_address for _, p in self.reads()}, {40001, 40002})
        self.assertEqual(len(self.reads()), 2)

    async def test_async_intermediate_and_compound_commands_queue_only_requested_fields(self):
        for values, nouns in (({'brightness': 90, 'color_temp': 4000}, {10, 29}),
                              ({'brightness': 128}, {10}), ({'color_temp': 3500}, {29})):
            self.mesh.frames.clear()
            with patch.object(self.runtime, '_observation_task', self):
                result = await self.runtime.control(258, values, confirm=False)
            self.assertFalse(result['confirmed'])
            self.assertTrue(result['feedback_pending'])
            self.assertEqual(self.reads(), [])
            self.assertEqual(len(self.setters()), len(nouns))
            await self.worker(lambda: not self.runtime._group_refresh)
            self.assertEqual({p.payload[1] for _, p in self.reads()}, nouns)
            self.assertEqual(len(self.reads()), 10 * len(nouns))

    async def test_repeated_identical_endpoint_does_not_postpone_or_restart_pending_refresh(self):
        await self.command()
        due = self.runtime._group_refresh_due
        revision = dict(self.runtime._group_refresh)
        for _ in range(5):
            self.now += 100
            await self.command()
            self.assertEqual(self.runtime._group_refresh_due, due)
            self.assertEqual(self.runtime._group_refresh, revision)
        await self.worker(lambda: not self.runtime._group_refresh)
        self.assertEqual(len(self.reads()), 10)
        self.assertEqual(len(self.setters()), 6)

    async def test_intervening_level_command_invalidates_identical_endpoint_coalescing(self):
        await self.command()
        due = self.runtime._group_refresh_due
        self.now += 100
        with patch.object(self.runtime, '_observation_task', self):
            await self.runtime.control(258, {'brightness': 128}, confirm=False)
        await self.command()
        self.assertGreater(self.runtime._group_refresh_due, due)
        self.assertEqual([p.payload[5] for _, p in self.setters()], [255, 128, 255])

    async def test_new_command_during_refresh_runs_between_members_and_preserves_new_revision(self):
        await self.command()
        second = None

        async def during_gap(delay):
            nonlocal second
            if delay == .15 and second is None and self.reads():
                second = self.now
                await self.command('OFF')

        await self.worker(lambda: not self.runtime._group_refresh and second is not None,
                          on_sleep=during_gap)
        setters = self.setters()
        self.assertEqual([p.payload[5] for _, p in setters], [255, 0])
        self.assertEqual(len([1 for when, _ in self.reads() if when < second]), 1)
        self.assertTrue(all(when - second >= GROUP_COMMAND_SETTLE_MS
                            for when, _ in self.reads() if when >= second))
        self.assertEqual(len(self.reads()), 20)  # newer revision survives the older pass

    async def test_command_during_periodic_pass_pauses_its_next_read(self):
        sent = None

        async def during_gap(delay):
            nonlocal sent
            if delay == .5 and sent is None:
                sent = self.now
                await self.command()

        await self.worker(lambda: self.runtime.observation_refresh_progress['passes'] >= 2,
                          settle=0, on_sleep=during_gap)
        self.assertIsNotNone(sent)
        self.assertTrue(all(when - sent >= GROUP_COMMAND_SETTLE_MS
                            for when, _ in self.reads() if when >= sent))
        self.assertEqual(len(self.setters()), 1)

    async def test_session_boundary_discards_feedback_without_replaying_setter(self):
        await self.command()
        self.runtime.session()
        self.assertEqual(self.runtime._group_refresh, {})
        self.assertEqual(self.runtime._group_refresh_due, 0)
        await self.worker(lambda: self.now >= 2000)
        self.assertEqual(self.reads(), [])
        self.assertEqual(len(self.setters()), 1)

    async def test_pending_membership_and_removed_member_are_not_queried(self):
        await self.command()
        self.store.begin('group_membership', 40001, {'group': 258})
        del self.runtime.core.devices[40002]
        await self.worker(lambda: not self.runtime._group_refresh)
        self.assertEqual({p.mcp_address for _, p in self.reads()}, set(range(40003, 40011)))
        self.assertEqual(len(self.setters()), 1)

    async def test_confirmed_group_readback_and_direct_light_semantics_are_preserved(self):
        result = await self.runtime.control(259, {'brightness': 128})
        self.assertTrue(result['confirmed'])
        self.assertEqual(len(self.reads()), 2)
        self.assertEqual(self.runtime._group_refresh, {})
        self.mesh.frames.clear()
        result = await self.runtime.control(40001, {'state': 'OFF'}, confirm=False)
        self.assertFalse(result['confirmed'])
        self.assertEqual(len(self.setters()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual(self.runtime._group_refresh, {})

    async def test_failed_or_cancelled_setter_is_not_saved_or_replayed(self):
        async def fail(_):
            raise RuntimeError('Synthetic transport uncertainty')
        self.mesh.on_send = fail
        with self.assertRaisesRegex(RuntimeError, 'Synthetic'):
            await self.command()
        self.assertEqual(self.runtime._group_refresh, {})
        self.mesh.on_send = None
        await self.worker(lambda: self.now >= 2000)
        self.assertEqual(len(self.setters()), 1)
        self.assertEqual(self.reads(), [])

        async def cancelled(_):
            raise asyncio.CancelledError()
        self.mesh.on_send = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.command()
        self.assertEqual(self.runtime._group_refresh, {})

    async def test_cancelled_background_send_cleans_locks_and_never_replays_setter(self):
        await self.command()
        self.now += GROUP_COMMAND_SETTLE_MS
        entered = asyncio.Event()
        async def blocked(packet):
            if packet.payload[0] == 1:
                entered.set()
                await asyncio.Future()
        self.mesh.on_send = blocked
        task = self.task(self.runtime.observation_refresh(settle=1000))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.setters()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertIsNone(self.runtime._observation_task)

    async def test_real_mqtt_worker_accepts_new_command_without_waiting_for_ten_readbacks(self):
        mqtt = MqttService(self.runtime, {'host': 'synthetic.invalid', 'tls': False})
        mqtt._connection(True)
        async def idle(_):
            await asyncio.Future()
        # Keep the actual worker at its idle boundary while checking the command
        # loop; avoid the Python3.9 wait_for completion/cancellation race in teardown.
        self.runtime._wait_observation_refresh = idle
        self.task(self.runtime.observation_refresh(settle=1000))
        await self.until(lambda: self.runtime._observation_task is not None)
        self.task(mqtt.command_loop())
        for state in ('ON', 'OFF'):
            mqtt.ingest(self.runtime.core.prefix + '/light/258/set',
                        json.dumps({'state': state}).encode(), False)
            expected = 1 if state == 'ON' else 2
            await self.until(lambda: mqtt.counts['commands_sent'] == expected)
        self.assertEqual([p.payload[5] for _, p in self.setters()], [255, 0])
        self.assertEqual(self.reads(), [])
        self.assertEqual(len(self.runtime._group_refresh), 10)

    async def test_no_observation_worker_preserves_existing_feedback(self):
        result = await self.runtime.control(258, {'state': 'ON'}, confirm=False)
        self.assertFalse(result['confirmed'])
        self.assertEqual(len(self.setters()), 1)
        self.assertEqual(len(self.reads()), 10)
        self.assertEqual(self.runtime._group_refresh, {})
        self.assertEqual(len(self.runtime.device_states()), 10)

    async def test_foreground_waits_for_current_send_but_not_missing_reply(self):
        await self.command()
        self.now += GROUP_COMMAND_SETTLE_MS
        self.mesh.silent = True
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(packet):
            if packet.payload[0] == 1:
                entered.set()
                await release.wait()
        self.mesh.on_send = blocked
        self.task(self.runtime.observation_refresh(settle=1000, gap=0))
        await entered.wait()
        off = self.task(self.command('OFF'))
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        release.set()
        await asyncio.wait_for(off, .5)
        self.assertEqual([p.payload[5] for _, p in self.setters()], [255, 0])
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual(self.runtime.waiters, [])


if __name__ == '__main__':
    unittest.main()
