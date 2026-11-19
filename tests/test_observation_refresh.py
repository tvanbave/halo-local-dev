"""Deterministic MQTT-independent observation lifecycle; synthetic mesh only."""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store

KEY = bytes(range(16))


class Mesh:
    ready, dropped = True, 0

    def __init__(self, owner):
        self.owner, self.sequence = owner, 100
        self.frames, self.silent = [], set()
        self.on_send = None

    async def send(self, frame):
        p = decrypt_mcp(frame, KEY)
        self.frames.append((self.owner.now, p))
        if p.mcp_address not in self.silent:
            noun, token = p.payload[1], p.payload[4]
            reply = bytes([1, noun, token]) + (b'\x80' if noun == 10 else b'\x01\x0b\xb8')
            self.reply(p.mcp_address, reply)
        if self.on_send:
            self.on_send(p)

    def reply(self, source, payload, address=0x8000, received_ms=None):
        self.sequence += 1
        self.owner.runtime.observe(encrypt_mcp(address.to_bytes(2, 'little') + b'\x73' + payload,
            KEY, self.sequence, source), self.owner.now if received_ms is None else received_ms)


class ObservationRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 40000 + n, 'product_id': 93, 'name': 'Light %d' % n,
             'exposed': bool(n % 2)} for n in range(1, 11)] + [
            {'device_id': 40100, 'product_id': 91, 'name': 'Local controller', 'exposed': False}],
            'groups': []})
        self.now = 10
        self.mesh = Mesh(self)
        self.runtime = Runtime(self.store, KEY, self.mesh, clock=lambda: self.now)
        self.runtime.mesh_ready = True
        self.runtime.session()
        self.messages = []
        self.runtime.sink = self.messages.append

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def run_worker(self, on_tick, **options):
        """Advance virtual time and cancel deliberately; no hardware or real delays."""
        original_sleep = asyncio.sleep
        ticks = 0
        async def sleep(delay):
            self.now += int(delay * 1000)
            await original_sleep(0)
        async def tick(delay):
            nonlocal ticks
            ticks += 1
            if ticks > 250:
                self.fail('Observation worker did not reach the expected boundary')
            if on_tick(ticks):
                raise asyncio.CancelledError()
            await sleep(delay)
        with patch('avionmesh_pi.runtime.asyncio.sleep', side_effect=sleep), \
                patch.object(self.runtime, '_wait_observation_refresh', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime.observation_refresh(settle=0, **options)
        self.assertIsNone(self.runtime._observation_task)
        self.assertEqual(self.runtime.waiters, [])

    async def test_startup_broadcast_512_lights_follows_up_only_missing_fields(self):
        from avionmesh_pi.lifecycle import reload_inventory
        ids = list(range(40001, 40513))
        self.store.import_inventory({'devices': [
            {'device_id': n, 'name': f'Light {n}', 'product_id': 93 if n % 2 else 134,
             'exposed': False} for n in ids], 'groups': []})
        reload_inventory(self.runtime)
        self.mesh.silent.add(0)  # A broadcast has no synthetic device at address zero.
        def answer(packet):
            if packet.mcp_address != 0:
                return
            noun = packet.payload[1]
            for target in ids:
                if (target, noun) in ((ids[0], 10), (ids[-1], 29)):
                    continue
                reply = bytes([1, noun, packet.payload[4]]) + (bytes([target % 256]) if noun == 10 else bytes([1, 11, 184]))
                self.mesh.reply(target, reply)
        self.mesh.on_send = answer
        await self.run_worker(lambda _: self.runtime.observation_refresh_progress['passes'] == 1,
                              broadcast_startup=True)
        requests = [(p.mcp_address, p.payload[1]) for _, p in self.mesh.frames]
        self.assertEqual(requests, [(0, 10), (0, 29), (ids[0], 10), (ids[-1], 29)])
        power, color = self.mesh.frames[:2]
        self.assertEqual(power[1].payload, bytes.fromhex('010a0000d00000000000'))
        self.assertEqual(color[1].payload, bytes.fromhex('011d0000000000000000'))
        self.assertGreaterEqual(color[0] - power[0], 1000)
        self.assertTrue(all(p.opcode == 0x73 for _, p in self.mesh.frames))
        status = self.runtime.status()['startup_broadcast']
        self.assertEqual((status['power_known'], status['color_known']), (511, 511))
        self.assertEqual(self.runtime._unanswered_power_reads, {})
        self.assertEqual(len(self.runtime.device_states()), 512)
        self.assertEqual(self.runtime.device_states()[str(ids[1])]['brightness']['value'], ids[1] % 256)
        self.assertEqual(self.runtime.device_states()[str(ids[0])]['brightness']['value'], 128)

    async def test_broadcast_silence_does_not_count_and_controller_reply_is_silent(self):
        self.mesh.silent.add(0)
        self.runtime.mqtt_connection(True)
        self.runtime.core.controllers.local_modes = {40100}
        self.mesh.on_send = lambda p: self.mesh.reply(40100, bytes([1, 10, 0xd0, 255])) if p.payload[1] == 10 else None
        async def advance(delay):
            self.now += int(delay * 1000)
        with patch.object(self.runtime, '_wait_observation_refresh', side_effect=advance):
            self.assertTrue(await self.runtime._startup_broadcast(self.runtime.generation))
        self.assertEqual(self.runtime._unanswered_power_reads, {})
        self.assertEqual(self.runtime.device_states(), {})
        self.assertEqual(self.runtime.controller_events, {})
        self.assertFalse(any('/event' in m['topic'] for m in self.messages))
        self.assertNotIn(40100, self.runtime.core.controllers.baselines)

    async def test_group_command_runs_during_startup_collection(self):
        inventory = self.store.inventory()
        inventory['groups'].append({'group_id': 258, 'name': 'Test', 'members': [40001, 40002], 'exposed': False, 'protected': False})
        self.store.import_inventory(inventory)
        self.mesh.silent.add(0)
        self.mesh.silent.add(258)
        entered = asyncio.Event()
        original_wait = self.runtime._wait_observation_refresh
        async def collecting(delay):
            entered.set()
            await original_wait(delay)
        with patch.object(self.runtime, '_wait_observation_refresh', side_effect=collecting):
            broadcast = asyncio.create_task(self.runtime._startup_broadcast(self.runtime.generation))
            self.runtime._observation_task = broadcast
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(self.runtime.operation.locked())
                self.assertIsNone(self.runtime._background_read_task)
                result = await asyncio.wait_for(self.runtime.control(258, {'state':'OFF'}, local=True, confirm=False), .5)
                self.assertTrue(result['sent'])
                self.assertTrue(await asyncio.wait_for(broadcast, .5))
            finally:
                broadcast.cancel()
                await asyncio.gather(broadcast, return_exceptions=True)
                self.runtime._observation_task = None
        self.assertEqual([(p.payload[0], p.payload[1]) for _, p in self.mesh.frames], [(1, 10), (0, 10)])
        self.assertEqual(self.mesh.frames[-1][1].payload[2:4], bytes.fromhex('0102'))
        self.assertTrue(self.runtime._group_refresh)
        self.assertEqual(self.runtime._unanswered_power_reads, {})

    async def test_one_time_worker_uses_partial_results_without_repeating_read(self):
        self.mesh.silent.add(0)
        def answer(packet):
            if packet.mcp_address == 0 and packet.payload[1] == 10:
                for target in range(40001, 40011):
                    self.mesh.reply(target, bytes([1, 10, 0xd0, 0]))
        self.mesh.on_send = answer
        async def advance(delay):
            self.now += int(delay * 1000)
        with patch.object(self.runtime, '_wait_observation_refresh', side_effect=advance):
            result = await self.runtime.startup_read(settle=0, gap=0, broadcast_startup=True)
        self.assertEqual(result['answered'], 10)
        self.assertEqual([(p.mcp_address, p.payload[1]) for _, p in self.mesh.frames],
                         [(0, 10), (0, 29)] + [(target, 29) for target in range(40001, 40011)])

    async def test_pending_or_changed_session_blocks_meshwide_read(self):
        self.mesh.silent.add(0)
        generation = self.runtime.generation
        self.mesh.on_send = lambda p: self.runtime.session()
        self.assertFalse(await self.runtime._startup_broadcast(generation))
        self.assertEqual(self.runtime.startup_broadcast_progress['state'], 'session_changed')
        self.assertEqual(len(self.mesh.frames), 1)
        self.store.begin('group_membership', 40003, {'group': 300})
        self.assertFalse(await self.runtime._startup_broadcast(self.runtime.generation))
        self.assertEqual(self.runtime.startup_broadcast_progress['state'], 'skipped')
        self.assertEqual(len(self.mesh.frames), 1)
        self.assertEqual(self.runtime._unanswered_power_reads, {})

    async def test_three_paced_passes_refresh_all_ten_lights_without_mqtt(self):
        await self.run_worker(lambda _: self.runtime.observation_refresh_progress['passes'] == 3)
        progress = self.runtime.status()['observation_refresh']
        self.assertEqual((progress['lights'], progress['answered'], progress['skipped']), (10, 10, 0))
        self.assertEqual(progress['last_completed_generation'], self.runtime.generation)
        self.assertEqual(len(self.runtime.device_states()), 10)
        self.assertEqual(len(self.mesh.frames), 60)
        self.assertFalse(self.runtime.mqtt_ready)
        self.assertEqual(self.messages, [])
        for _, p in self.mesh.frames:
            self.assertIn(p.payload[:2], (b'\x01\x0a', b'\x01\x1d'))
        # Every field and light has deliberate spacing; later passes wait30s.
        self.assertEqual(self.mesh.frames[1][0] - self.mesh.frames[0][0], 150)
        self.assertEqual(self.mesh.frames[2][0] - self.mesh.frames[1][0], 500)
        self.assertGreaterEqual(self.mesh.frames[20][0] - self.mesh.frames[19][0], 30000)

    async def test_reconnect_and_logical_group_boundary_rearm_same_worker(self):
        phase = 0
        def tick(_):
            nonlocal phase
            p = self.runtime.observation_refresh_progress
            if phase == 0 and p['passes'] == 1:
                phase = 1
                self.runtime.mesh_ready = self.mesh.ready = False
                self.runtime.session()
                self.assertEqual(self.runtime.device_states(), {})
                self.assertEqual(len(self.mesh.frames), 20)
            elif phase == 1:
                self.assertEqual(len(self.mesh.frames), 20)
                phase = 2
                self.runtime.mesh_ready = self.mesh.ready = True
                self.runtime.session()
            elif phase == 2 and p['passes'] == 2:
                phase = 3
                self.runtime.session()  # membership edits invalidate this same way
                self.assertEqual(self.runtime.device_states(), {})
            return p['passes'] == 3
        await self.run_worker(tick)
        self.assertEqual(len(self.mesh.frames), 60)
        self.assertEqual(len(self.runtime.device_states()), 10)

    async def test_only_a_completed_power_reply_wait_can_mark_a_light_unanswered(self):
        self.mesh.silent.add(40001)
        self.store.begin('group_membership', 40003, {'group': 300})
        self.runtime.startup_read_progress = {'state': 'running'}
        self.assertEqual(self.runtime.power_read_status()['40001'], 'waiting')
        self.assertNotIn('40003', self.runtime.power_read_status())
        self.assertNotIn('40100', self.runtime.power_read_status())
        with patch.object(self.mesh, 'send', side_effect=asyncio.TimeoutError):
            await self.runtime.read_light(40001, brightness_only=True)
        self.assertEqual(self.runtime.power_read_status()['40001'], 'waiting')
        wait_for = asyncio.wait_for
        async def short_wait(future, timeout):
            return await wait_for(future, .02)
        with patch('avionmesh_pi.runtime.asyncio.wait_for', side_effect=short_wait):
            # A one-shot/manual read cannot promise another scheduled check.
            await self.runtime.read_light(40001, brightness_only=True)
            self.assertEqual(self.runtime.power_read_status()['40001'], 'unconfirmed')
            with patch.object(self.runtime, '_observation_task', asyncio.current_task()):
                self.now += 3_600_000
                self.assertEqual(self.runtime.power_read_status()['40001'], 'retry_pending')
                await self.runtime.read_light(40001, brightness_only=True)
                self.assertEqual(self.runtime.power_read_status()['40001'], 'retry_pending')
                # A colour report proves the light spoke but supplies no power reading.
                self.mesh.reply(40001, bytes([1, 29, 0, 1, 11, 184]))
                self.assertEqual(self.runtime.power_read_status()['40001'], 'retry_pending')
                await self.runtime.read_light(40001, brightness_only=True)
                self.assertEqual(self.runtime.power_read_status()['40001'], 'no_reply')
                self.assertEqual(self.runtime.power_read_status()['40002'], 'waiting')
                self.mesh.reply(40001, bytes([1, 10, 0, 0]))
                self.assertNotIn('40001', self.runtime.power_read_status())
                self.runtime.session()
                await self.runtime.read_light(40001, brightness_only=True)
                self.assertEqual(self.runtime.power_read_status()['40001'], 'retry_pending')
                self.mesh.ready = False
                self.assertEqual(self.runtime.power_read_status(), {})
                self.runtime.session()
                self.mesh.ready = True
                self.assertEqual(self.runtime.power_read_status()['40001'], 'waiting')
        self.runtime.startup_read_progress['state'] = 'done'
        self.assertEqual(self.runtime.power_read_status(), {})

    async def test_full_inventory_startup_sweep_resumes_after_group_feedback(self):
        from avionmesh_pi.lifecycle import reload_inventory
        ids = list(range(40001, 40513))
        self.store.import_inventory({'devices': [
            {'device_id': target, 'name': f'Light {target}', 'product_id': 93, 'exposed': False}
            for target in ids], 'groups': []})
        reload_inventory(self.runtime)
        visited = []

        async def read(target, *, field_gap):
            visited.append(target)
            if target in (40004, 40231, 40505):
                self.runtime._queue_light_refresh(301, None, [ids[0]], settle_ms=0)
            if target == ids[-1]:
                self.assertGreater(self.now, 900_000)
                self.assertEqual(self.runtime.power_read_status()[str(target)], 'waiting')
                self.mesh.reply(target, bytes([1, 10, 0, 0]))
            return {'observed': {}}

        with patch.object(self.runtime, 'read_light', side_effect=read):
            await self.run_worker(lambda _: len(visited) >= len(ids), gap=2)
        self.assertEqual(visited, ids)
        self.assertNotIn(str(ids[-1]), self.runtime.power_read_status())
        self.assertEqual(len(self.mesh.frames), 3)

    async def test_session_change_during_read_abandons_remaining_old_pass(self):
        def invalidate(_):
            self.mesh.on_send = None
            self.runtime.session()
        self.mesh.on_send = invalidate
        await self.run_worker(lambda _: self.runtime.observation_refresh_progress['passes'] == 1)
        self.assertEqual(len(self.mesh.frames), 21)
        self.assertEqual(len(self.runtime.device_states()), 10)
        self.assertEqual(self.runtime.observation_refresh_progress['answered'], 10)

    async def test_pending_targets_are_skipped_and_interactive_lock_gets_turns(self):
        self.store.begin('claim', 40001, {'uuid': '00' * 16})
        self.store.begin('group_membership', 40002, {'group': 300})
        await self.runtime.operation.acquire()
        def tick(number):
            if number == 1:
                self.assertEqual(self.mesh.frames, [])
                self.runtime.operation.release()
            return self.runtime.observation_refresh_progress['passes'] == 1
        await self.run_worker(tick)
        progress = self.runtime.observation_refresh_progress
        self.assertEqual((progress['answered'], progress['skipped']), (8, 2))
        self.assertFalse({40001, 40002} & {p.mcp_address for _, p in self.mesh.frames})

    async def test_operation_can_run_between_lights_and_blocks_overlapping_reads(self):
        original = self.runtime.read_light
        seen = []
        async def read(target, **kwargs):
            result = await original(target, **kwargs)
            self.assertFalse(self.runtime.operation.locked())
            if not seen:
                await self.runtime.operation.acquire()
            seen.append(target)
            return result
        with patch.object(self.runtime, 'read_light', side_effect=read):
            await self.run_worker(lambda _: self.runtime.observation_refresh_progress['passes'] == 1)
        self.assertEqual(len(self.mesh.frames), 2)
        self.assertEqual(self.runtime.observation_refresh_progress['skipped'], 9)
        self.runtime.operation.release()

    async def test_idle_state_recovers_after_interactive_operation_finishes(self):
        phase = 0
        original = self.runtime.read_light
        async def read(target, **kwargs):
            nonlocal phase
            result = await original(target, **kwargs)
            if target == 40010 and phase == 0:
                await self.runtime.operation.acquire()
                phase = 1
            return result
        def tick(_):
            nonlocal phase
            state = self.runtime.observation_refresh_progress['state']
            if phase == 1 and state == 'waiting_operation':
                self.runtime.operation.release()
                phase = 2
            elif phase == 2:
                self.assertEqual(state, 'idle')
                return True
            return False
        with patch.object(self.runtime, 'read_light', side_effect=read):
            await self.run_worker(tick)

    async def test_long_offline_wait_and_repeated_sessions_create_no_other_worker(self):
        self.runtime.mesh_ready = self.mesh.ready = False
        def tick(number):
            if number < 8:
                self.runtime.session()
                self.assertEqual(self.mesh.frames, [])
            elif number == 8:
                self.now += 700000  # longer than legacy one-time startup timeout
                self.runtime.mesh_ready = self.mesh.ready = True
                self.runtime.session()
            return self.runtime.observation_refresh_progress['passes'] == 1
        await self.run_worker(tick)
        self.assertEqual(len(self.mesh.frames), 20)

    async def test_duplicate_worker_refused_and_cancel_cleans_exchange_lock(self):
        self.mesh.silent.add(40001)
        task = asyncio.create_task(self.runtime.observation_refresh(settle=0, gap=0))
        for _ in range(20):
            await asyncio.sleep(0)
            if self.runtime.waiters:
                break
        self.assertEqual(len(self.runtime.waiters), 1)
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            await self.runtime.observation_refresh()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runtime.operation.locked())
        self.assertEqual(self.runtime.waiters, [])
        self.assertIsNone(self.runtime._observation_task)

    async def test_probe_baseline_is_token_correlated_and_silent(self):
        self.runtime.core.controllers.local_modes = {40100}
        self.mesh.silent.add(40100)
        async def answer():
            while not self.runtime.waiters:
                await asyncio.sleep(0)
            token = self.runtime.local_baseline_query[1]
            self.mesh.reply(40100, bytes([1, 10, token, 90]), received_ms=self.now - 1)
            self.assertNotIn(40100, self.runtime.core.controllers.baselines)
            self.assertFalse(self.runtime.waiters[0][1].done())
            self.mesh.reply(40100, bytes([1, 10, token + 1, 100]))
            self.assertNotIn(40100, self.runtime.core.controllers.baselines)
            self.assertFalse(self.runtime.waiters[0][1].done())
            self.mesh.reply(40100, bytes([1, 10, token, 120]), address=0)
            self.assertNotIn(40100, self.runtime.core.controllers.baselines)
            self.mesh.reply(40100, bytes([1, 10, token, 128]))
        responder = asyncio.create_task(answer())
        await self.runtime.read_controller_baseline(40100)
        await responder
        self.assertEqual(self.runtime.core.controllers.baselines[40100][1], 128)
        self.assertEqual(self.runtime.controller_events, {})
        self.assertEqual(self.messages, [])
        self.assertIsNone(self.runtime.local_baseline_query)

    async def test_missing_controller_probe_cadence_fairness_and_token_exhaustion(self):
        self.runtime.core.controllers.local_modes = {40100}
        schedule, attempts = {}, []
        async def fail(target, **kwargs):
            attempts.append(self.now)
            raise asyncio.TimeoutError()
        with patch.object(self.runtime, 'read_controller_baseline', side_effect=fail):
            for seconds in (0, 4, 5, 9, 10, 14, 15, 74, 75):
                self.now = seconds * 1000
                await self.runtime._probe_local_controllers(self.runtime.generation, schedule)
        self.assertEqual(attempts, [5000, 10000, 15000, 75000])
        self.runtime._baseline_token = 255
        with self.assertRaisesRegex(RuntimeError, 'token space exhausted'):
            await self.runtime.read_controller_baseline(40100)
        self.assertEqual(self.mesh.frames, [])
        await self.run_worker(lambda _: self.runtime.observation_refresh_progress['passes'] == 1)
        self.assertEqual(self.runtime.observation_refresh_progress['answered'], 10)
        self.assertEqual(self.runtime.observation_refresh_progress['controller_probe_paused'], 'token_exhausted')
        self.runtime.session()
        self.assertEqual(self.runtime._baseline_token, 0)

    async def test_missing_controller_gets_one_silent_baseline_then_no_more_probes(self):
        self.runtime.core.controllers.local_modes = {40100}
        await self.run_worker(lambda _: self.runtime.observation_refresh_progress['passes'] == 2)
        probes = [p for _, p in self.mesh.frames if p.mcp_address == 40100]
        self.assertEqual(len(probes), 1)
        self.assertNotEqual(probes[0].payload[4], 0)
        self.assertIn(40100, self.runtime.core.controllers.baselines)
        self.assertEqual(self.runtime.controller_events, {})


if __name__ == '__main__':
    unittest.main()
