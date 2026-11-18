"""Synthetic timing, replay and read-recovery checks; no physical controller I/O."""
import asyncio
import tempfile
import unittest

from avionmesh_pi.accessory import AccessoryOnboarding
from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.onboarding import KeypadOnboarding
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_editor import EditorTransport
from test_service import KEY, INVENTORY


class KeypadAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.now = [10]
        self.transport = EditorTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now[0])
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        self.wakes = []
        self.sleeps = []
        self.keypad = KeypadService(self.runtime, sleeper=self.sleep)
        # Temporary operation observers must leave the exact service-owned
        # callback set intact, including battery cache contradiction monitoring.
        self.permanent_packet_observers = tuple(self.runtime.packet_observers)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now[0] += round(seconds * 1000)
        if self.keypad.progress.get(40001) == 'settling':
            return
        def wake():
            self.wakes.append(self.now[0])
            self.runtime.observe(encrypt_mcp(b'\0\x80\x73\x0b\x1c' + bytes(5),
                KEY, self.transport.sequence, 40001), self.now[0])
            self.transport.sequence += 1
        asyncio.get_running_loop().call_soon(wake)

    def battery(self):
        inventory = self.store.inventory()
        inventory['devices'][0]['product_id'] = 126
        self.store.import_inventory(inventory)
        from avionmesh_pi.lifecycle import reload_inventory
        reload_inventory(self.runtime)

    async def test_battery_settles_and_waits_new_wake_after_slow_complete_tables(self):
        self.battery()
        self.transport.rows[0] = bytes.fromhex('01300001ffff0000')
        send = self.transport.send
        writes = []
        async def delayed(frame):
            payload = decrypt_mcp(frame, KEY).payload
            self.assertGreaterEqual(self.now[0] - self.wakes[-1], 150)
            if payload[0] in (1, 4):
                self.now[0] += 300
            else:
                self.assertLessEqual(self.now[0] - self.wakes[-1], 1500)
                writes.append((len(self.wakes), self.now[0]))
            await send(frame)
        self.transport.send = delayed
        before = await self.keypad.read(40001)
        self.assertTrue((await self.keypad.apply(40001, 1, 'native', 291, 100,
            before['revision'], allowed_models=(126,)))['verified'])
        self.assertEqual([wake for wake, _ in writes], [5, 5])
        self.assertEqual(len(self.wakes), 7)

    async def test_durable_token_delay_expires_admission_without_setter(self):
        before = await self.keypad.read(40001)
        token = self.keypad._token
        def delayed_token():
            value = token()
            self.now[0] += 751
            return value
        self.keypad._token = delayed_token
        with self.assertRaisesRegex(RuntimeError, 'Fresh-slot'):
            await self.keypad.apply(40001, 1, 'native', 291, 100,
                before['revision'], allowed_models=(127,))
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_sequence_allocation_delay_rechecks_admission_at_radio_edge(self):
        before = await self.keypad.read(40001)
        allocate = self.store.allocate_sequence
        def delayed_sequence():
            sequence = allocate()
            if self.store.get('table_token', 0):
                self.now[0] += 751
            return sequence
        self.store.allocate_sequence = delayed_sequence
        with self.assertRaisesRegex(RuntimeError, 'Fresh-slot'):
            await self.keypad.apply(40001, 1, 'native', 291, 100,
                before['revision'], allowed_models=(127,))
        self.assertEqual(self.transport.writes, [])
        self.assertFalse(self.runtime.mesh_ready)

    async def test_wired_read_latency_does_not_consume_receipt_freshness(self):
        self.transport.rows[0] = bytes.fromhex('01300001ffff0000')
        before = await self.keypad.read(40001)
        send = self.transport.send
        token = self.keypad._token

        async def relay_latency(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] == 1:
                self.now[0] += 404
            elif payload[0] == 2 and not payload[5] & 0x20:
                self.now[0] += 276
            await send(frame)

        def durable_overhead():
            value = token()
            self.now[0] += 51
            return value

        self.transport.send = relay_latency
        self.keypad._token = durable_overhead
        result = await self.keypad.apply(40001, 1, 'native', 291, 100,
            before['revision'], allowed_models=(127,))
        self.assertTrue(result['verified'])
        self.assertEqual([p[5] for p in self.transport.writes], [1, 0x21])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_wired_freshness_uses_exact_reply_not_later_reply_or_resume(self):
        send = self.transport.send

        async def delayed_resume(frame):
            await send(frame)
            self.now[0] += 751
            # Another matching reply must not renew the first reply's age.
            await send(frame)

        self.transport.send = delayed_resume
        with self.assertRaisesRegex(RuntimeError, 'Fresh-slot'):
            await self.keypad.prepare_write(40001, 1, False, self.runtime.generation)
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_wired_receipt_observer_is_removed_on_cancel(self):
        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError()

        self.runtime.exchange = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.keypad.prepare_write(40001, 1, False, self.runtime.generation)
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)
        self.assertEqual(self.transport.writes, [])

    async def test_battery_read_latency_does_not_consume_receipt_freshness(self):
        self.battery()
        self.transport.rows[0] = bytes.fromhex('01300001ffff0000')
        before = await self.keypad.read(40001)
        send = self.transport.send

        async def relay_latency(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] == 1:
                self.now[0] += 404
            elif payload[0] == 2 and not payload[5] & 0x20:
                self.now[0] += 400
            await send(frame)

        self.transport.send = relay_latency
        result = await self.keypad.apply(40001, 1, 'native', 291, 100,
            before['revision'], allowed_models=(126,))
        self.assertTrue(result['verified'])
        self.assertEqual([p[5] for p in self.transport.writes], [1, 0x21])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_battery_late_resume_cannot_renew_exact_receipt(self):
        self.battery()
        send = self.transport.send
        async def delayed_resume(frame):
            await send(frame)
            self.now[0] += 751
            await send(frame)  # A new matching reply cannot renew the first.
        self.transport.send = delayed_resume
        with self.assertRaisesRegex(RuntimeError, 'Fresh-slot'):
            await self.keypad.prepare_write(40001, 1, True, self.runtime.generation)
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_battery_selected_read_retains_request_deadline(self):
        self.battery()
        send = self.transport.send
        async def delayed_reply(frame):
            self.now[0] += 751
            await send(frame)
        self.transport.send = delayed_reply
        with self.assertRaisesRegex(RuntimeError, 'Selected-slot read deadline'):
            await self.keypad.prepare_write(40001, 1, True, self.runtime.generation)
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_battery_receipt_observer_is_removed_on_cancel(self):
        self.battery()
        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError()
        self.runtime.exchange = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.keypad.prepare_write(40001, 1, True, self.runtime.generation)
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)
        self.assertEqual(self.transport.writes, [])

    async def test_battery_secondary_expiry_keeps_single_primary_and_uncertain_receipt(self):
        self.battery()
        self.transport.rows[0] = bytes.fromhex('01300001ffff0000')
        before = await self.keypad.read(40001)
        send = self.transport.send
        async def slow_primary(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] == 1:
                self.now[0] += 404
            elif payload[0] == 2 and not payload[5] & 0x20:
                self.now[0] += 751
            await send(frame)
        self.transport.send = slow_primary
        with self.assertRaisesRegex(ValueError, 'does not match exact proposal'):
            await self.keypad.apply(40001, 1, 'native', 291, 100,
                before['revision'], allowed_models=(126,))
        self.assertEqual(len(self.transport.writes), 1)
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_battery_fresh_slot_cannot_extend_wake_cap(self):
        self.battery()
        self.transport.rows[0] = bytes.fromhex('01300001ffff0000')
        before = await self.keypad.read(40001)
        send = self.transport.send
        async def within_slot_but_past_wake(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] == 1 and self.keypad.progress.get(40001) == 'settling':
                self.now[0] += 700
            elif payload[0] == 2 and not payload[5] & 0x20:
                self.now[0] += 700
            await send(frame)
        self.transport.send = within_slot_but_past_wake
        # Secondary would be 700 ms after the exact slot reply, but 1550 ms
        # after wake. It must not transmit even though slot evidence is fresh.
        with self.assertRaisesRegex(ValueError, 'does not match exact proposal'):
            await self.keypad.apply(40001, 1, 'native', 291, 100,
                before['revision'], allowed_models=(126,))
        self.assertEqual(len(self.transport.writes), 1)
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)

    async def test_secondary_keeps_original_selected_slot_deadline(self):
        # Start with an empty selected slot and a different unoccupied group.
        self.transport.rows[0] = bytes.fromhex('01300001ffff0000')
        before = await self.keypad.read(40001)
        send = self.transport.send
        async def slow_primary(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] == 2 and not payload[5] & 0x20:
                self.now[0] += 751
            await send(frame)
        self.transport.send = slow_primary
        # The follow-up record is never sent late. Readback then decides: this
        # synthetic keypad stored nothing, so the receipt stays uncertain.
        with self.assertRaisesRegex(ValueError, 'does not match exact proposal'):
            await self.keypad.apply(40001, 1, 'native', 291, 100,
                before['revision'], allowed_models=(127,))
        self.assertEqual(len(self.transport.writes), 1)
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_wired_read_retries_are_bounded_and_preserve_complete_rounds(self):
        exchange = self.runtime.exchange
        seen = []
        failures = [2]
        async def flaky(command, predicate, **kwargs):
            slot = command[8] if command[3] == 1 else 0
            seen.append((slot, kwargs['timeout']))
            if slot == 2 and failures[0]:
                failures[0] -= 1
                raise asyncio.TimeoutError()
            return await exchange(command, predicate, **kwargs)
        self.runtime.exchange = flaky
        await self.keypad.read(40001)
        self.assertEqual([s for s, _ in seen], [0,1,2,2,2,3,4,0,1,2,3,4])
        self.assertTrue(all(timeout == 8 for _, timeout in seen))
        self.assertEqual(self.transport.writes, [])
        failures[0] = 3
        seen.clear()
        with self.assertRaises(asyncio.TimeoutError):
            await self.keypad.read(40001)
        self.assertEqual([s for s, _ in seen], [0,1,2,2,2])
        self.assertNotIn(40001, self.store.bindings())

    async def test_battery_timeout_restarts_count_on_new_wake_and_has_retry_limit(self):
        self.battery()
        exchange = self.runtime.exchange
        seen = []
        failures = [1]
        async def flaky(command, predicate, **kwargs):
            slot = command[8] if command[3] == 1 else 0
            seen.append((slot, len(self.wakes), kwargs['timeout']))
            if slot == 2 and failures[0]:
                failures[0] -= 1
                raise asyncio.TimeoutError()
            return await exchange(command, predicate, **kwargs)
        self.runtime.exchange = flaky
        await self.keypad.read(40001)
        self.assertEqual([s for s, _, _ in seen], [0,1,2,0,1,2,3,4,0,1,2,3,4])
        self.assertEqual([wake for s, wake, _ in seen if s == 0], [1,2,3])
        self.assertTrue(all(timeout == .75 for _, _, timeout in seen))
        self.assertTrue(all(b-a >= 20000 for a, b in zip(self.wakes, self.wakes[1:])))
        failures[0] = 3
        seen.clear()
        with self.assertRaises(asyncio.TimeoutError):
            await self.keypad.read(40001)
        self.assertEqual([s for s, _, _ in seen], [0,1,2] * 3)
        self.assertEqual(self.transport.writes, [])

    async def test_wake_cancellation_and_session_change_remove_observers(self):
        async def no_wake(seconds):
            pass
        self.keypad.sleep = no_wake
        task = asyncio.create_task(self.keypad.wait_wake(40001, self.runtime.generation))
        await asyncio.sleep(0)
        self.assertEqual(len(self.runtime.waiters), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)
        async def changed_session(seconds):
            self.runtime.session()
        self.keypad.sleep = changed_session
        with self.assertRaisesRegex(RuntimeError, 'earlier session'):
            await self.keypad.wait_wake(40001, self.runtime.generation)
        self.assertEqual(self.runtime.waiters, [])

    async def test_battery_awake_deadline_is_independent_of_selected_slot(self):
        self.keypad.wake_at[40001] = self.now[0]
        self.keypad.wake_sequences[40001] = (self.runtime.generation, 1)
        self.now[0] += 1499
        checked = self.now[0]
        self.keypad.admit_write(40001, True, checked, self.runtime.generation)
        self.now[0] += 2
        with self.assertRaisesRegex(RuntimeError, 'Battery wake'):
            self.keypad.admit_write(40001, True, checked, self.runtime.generation)

    async def test_onboarding_secondary_expiry_keeps_uncertain_receipt_and_single_setter(self):
        self.runtime.capabilities = {'wired_onboarding':True, 'inventory_complete':True}
        send = self.transport.send
        async def slow_primary(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] == 2 and not payload[5] & 0x20:
                self.now[0] += 751
            await send(frame)
        self.transport.send = slow_primary
        with self.assertRaisesRegex(ValueError, 'did not match complete proposal'):
            await KeypadOnboarding(self.runtime, self.keypad).setup(40001)
        self.assertEqual(len(self.transport.writes), 1)
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_setter_timeout_never_uses_read_retry_policy(self):
        self.transport.rows[0] = bytes.fromhex('01300001ffff0000')
        before = await self.keypad.read(40001)
        send = self.transport.send
        attempts = []
        async def timeout_setter(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] == 2:
                attempts.append(payload)
                raise asyncio.TimeoutError()
            await send(frame)
        self.transport.send = timeout_setter
        # A setter with no reply is never repeated; the unchanged readback
        # leaves the receipt uncertain for explicit review.
        with self.assertRaisesRegex(ValueError, 'does not match exact proposal'):
            await self.keypad.apply(40001, 1, 'native', 291, 100,
                before['revision'], allowed_models=(127,))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_session_change_during_wake_settle_prevents_count(self):
        sleep = self.keypad.sleep
        async def invalidate_settle(seconds):
            if self.keypad.progress.get(40001) == 'settling':
                self.runtime.session()
            await sleep(seconds)
        self.keypad.sleep = invalidate_settle
        with self.assertRaisesRegex(RuntimeError, 'earlier session'):
            await self.keypad._round(40001, True)
        self.assertEqual(self.store.get('sequence', 0), 0)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(tuple(self.runtime.packet_observers), self.permanent_packet_observers)


class NativeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [{'device_id':40001, 'product_id':91,
            'name':'Synthetic', 'exposed':False}], 'groups':[]})
        self.transport = EditorTransport()
        self.now = [10]
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda:self.now[0])
        self.runtime.mesh_ready = True
        self.runtime.session()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def emit(self, sequence, group=291, level=128):
        payload = b'\0\x0a' + group.to_bytes(2, 'big') + bytes([1,0,level,0,0,0])
        self.runtime.observe(encrypt_mcp(b'\0\x80\x73' + payload, KEY, sequence, 40001), self.now[0])

    async def test_older_native_frame_cannot_rewrite_baseline_or_satisfy_waiters(self):
        self.emit(100)
        seen = []
        self.runtime.packet_observers.append(lambda packet, when:seen.append(packet.sequence))
        future = asyncio.get_running_loop().create_future()
        self.runtime.waiters.append((lambda p:True, future, self.now[0]))
        self.emit(99, 292)
        self.emit(100, 293)
        self.assertEqual(seen, [])
        self.assertFalse(future.done())
        self.assertEqual(self.runtime.native_observations[40001][0]['target'], 291)
        self.assertNotIn(292, self.store.get('known_targets', []))
        # Unrelated token-correlated responses are not native behavior evidence.
        self.runtime.observe(encrypt_mcp(b'\0\x80\x73\x00\x1b\x05\x00',
            KEY, 98, 40001), self.now[0])
        self.assertEqual((await future).sequence, 98)
        self.emit(101, 292)
        self.assertEqual(self.runtime.native_observations[40001][0]['target'], 292)

    async def test_native_sequence_wrap_and_session_reset(self):
        self.emit(0xfffffe)
        self.emit(1, 292)
        self.assertEqual(self.runtime.native_observations[40001][0]['target'], 292)
        self.emit(0xffffff, 293)
        self.assertEqual(self.runtime.native_observations[40001][0]['target'], 292)
        self.runtime.session()
        self.emit(0xffffff, 293, 129)
        self.assertEqual(self.runtime.native_observations[40001][0]['target'], 293)

    async def test_duplicate_native_frame_evicted_from_core_cannot_restore_old_target(self):
        self.emit(100, 291)
        self.emit(101, 292)
        for sequence in range(200, 265):
            self.runtime.observe(encrypt_mcp(b'\0\x80\x73\x06\xff\x01\x01\0\0\0',
                KEY, sequence, 40001), self.now[0])
        events = self.runtime.core.counts['controller_events']
        self.emit(100, 291)
        self.assertEqual(self.runtime.core.counts['controller_events'], events)
        self.assertEqual(self.runtime.core.controllers.native_targets[40001], 292)
        self.assertEqual(self.runtime.native_observations[40001][0]['target'], 292)

    async def test_stale_conflicting_target_cannot_poison_newer_assignment_proof(self):
        identifier = self.store.begin('accessory_setup', 40001,
            {'reservation':{'group_id':291}, 'product_id':91})
        self.emit(100, 292)
        async def dispatch(command, **kwargs):
            self.emit(101, 291)
            self.emit(99, 292)
        self.runtime.send = dispatch
        result = await AccessoryOnboarding(self.runtime).observe_native(identifier,
            dispatch=b'synthetic', timeout=.1)
        self.assertEqual(result['sequence'], 101)
        self.assertEqual(self.runtime.native_observations[40001][0]['target'], 291)
