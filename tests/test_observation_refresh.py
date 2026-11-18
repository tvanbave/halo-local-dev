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
