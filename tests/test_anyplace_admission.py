"""Synthetic model-81 wake/dispatch boundaries; no hardware qualification."""
import asyncio
import tempfile
import unittest

from avionmesh_pi.dimmer import DimmerEditor
from avionmesh_pi.lifecycle import reload_inventory
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY


class AnyplaceTransport:
    ready = True
    dropped = 0

    def __init__(self):
        self.runtime = None
        self.frames = []
        self.sequence = 10

    def emit(self, payload, *, sequence=None, source=40001, received=None):
        if sequence is None:
            sequence = self.sequence
            self.sequence = ((self.sequence + 1) & 0xffffff) or 1
        frame = encrypt_mcp(b'\0\x80\x73' + payload, KEY, sequence, source)
        self.runtime.observe(frame, self.runtime.clock() if received is None else received)
        return frame

    def native(self, group=293, level=128):
        return self.emit(b'\0\x0a' + group.to_bytes(2, 'big') + bytes([0, level, 0, 0, 0, 0]))

    def wake(self, **kwargs):
        return self.emit(b'\x0b\x1c' + bytes(5), **kwargs)

    async def send(self, frame):
        self.frames.append(frame)
        packet = decrypt_mcp(frame, KEY)
        assert self.runtime.store.journals()[-1]['status'] == 'attempted'
        assert packet.payload[:2] == b'\0\x1b'
        self.native(int.from_bytes(packet.payload[5:7], 'big'))

    async def disconnect(self):
        self.ready = False


class AnyplaceAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({
            'devices': [{'device_id': 40001, 'product_id': 81,
                         'name': 'Synthetic Anyplace', 'exposed': True}],
            'groups': [{'group_id': 291, 'name': 'Synthetic destination',
                        'members': [], 'exposed': False, 'protected': False}]})
        self.now = 100
        self.transport = AnyplaceTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities['anyplace_editor'] = True
        self.runtime.session()
        self.editor = DimmerEditor(self.runtime)
        self.transport.native()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_accessory_editor_gate_does_not_enable_anyplace_writes(self):
        self.runtime.capabilities = {'dimmer_editor': True}
        with self.assertRaises(ValueError):
            await self.editor.assign(40001, 291, timeout=.01)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def start(self, **kwargs):
        task = asyncio.create_task(self.editor.assign(40001, 291, timeout=.2, **kwargs))
        await asyncio.sleep(0)
        self.assertTrue(self.runtime.waiters)
        return task

    async def start_wake(self, timeout=.2):
        task = asyncio.create_task(self.editor.wait_wake(40001,
            self.runtime.native_observations[40001], self.runtime.generation, timeout))
        await asyncio.sleep(0)
        return task

    def assert_clean(self):
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.runtime.packet_observers, [])

    async def test_one_fresh_wake_allows_one_setter_and_requires_acceptance(self):
        task = await self.start()
        self.transport.wake()
        self.now += 250
        result = await task
        self.assertTrue(result['desired_behavior_observed'])
        self.assertTrue(result['acceptance_required'])
        self.assertFalse(result['assignment_read_back'])
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assert_clean()

    async def test_delayed_task_cannot_renew_received_wake_deadline(self):
        task = await self.start()
        self.transport.wake()
        self.now += 251
        with self.assertRaisesRegex(RuntimeError, 'Wake dispatch'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals()[-1]['status'], 'cancelled')
        self.assert_clean()

    async def test_later_matching_wake_does_not_replace_selected_receipt(self):
        task = await self.start()
        self.transport.wake()
        self.now += 200
        self.transport.wake()
        self.now += 51
        with self.assertRaisesRegex(RuntimeError, 'Wake dispatch'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_sequence_storage_delay_rechecks_wake_at_radio_edge(self):
        allocate = self.store.allocate_sequence

        def delayed():
            sequence = allocate()
            self.now += 251
            return sequence

        self.store.allocate_sequence = delayed
        task = await self.start()
        self.transport.wake()
        with self.assertRaisesRegex(RuntimeError, 'Wake dispatch'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertFalse(self.runtime.mesh_ready)
        self.assert_clean()

    async def test_original_baseline_cannot_expire_during_sequence_storage(self):
        self.now += 14999
        allocate = self.store.allocate_sequence

        def delayed():
            sequence = allocate()
            self.now += 2
            return sequence

        self.store.allocate_sequence = delayed
        task = await self.start()
        self.transport.wake()
        with self.assertRaisesRegex(ValueError, 'Original target evidence'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assert_clean()

    async def test_session_change_after_wake_never_dispatches(self):
        task = await self.start()
        self.transport.wake()
        self.runtime.session()
        with self.assertRaisesRegex(RuntimeError, 'earlier session'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_session_change_during_sequence_storage_never_dispatches(self):
        allocate = self.store.allocate_sequence

        def changed():
            sequence = allocate()
            self.runtime.session()
            return sequence

        self.store.allocate_sequence = changed
        task = await self.start()
        self.transport.wake()
        with self.assertRaisesRegex(RuntimeError, 'earlier session'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_baseline_drift_at_radio_edge_never_dispatches(self):
        allocate = self.store.allocate_sequence

        def changed():
            sequence = allocate()
            self.transport.native(294)
            return sequence

        self.store.allocate_sequence = changed
        task = await self.start()
        self.transport.wake()
        with self.assertRaisesRegex(ValueError, 'Original target evidence'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_cancelled_wake_wait_removes_waiter_and_observer(self):
        task = await self.start()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals()[-1]['status'], 'cancelled')
        self.assert_clean()

    async def test_expired_duplicate_cache_does_not_reuse_consumed_wake(self):
        task = await self.start_wake()
        frame = self.transport.wake()
        self.assertEqual(await task, self.now)
        self.now += 6000
        task = await self.start_wake()
        self.runtime.observe(frame, self.now)
        await asyncio.sleep(.001)
        self.assertFalse(task.done())
        self.now += 1
        self.transport.wake()
        self.assertEqual(await task, self.now)
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_cancel_after_receipt_does_not_unconsume_wake(self):
        task = await self.start_wake()
        frame = self.transport.wake()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.now += 6000
        task = await self.start_wake(timeout=.01)
        self.runtime.observe(frame, self.now)
        with self.assertRaises(asyncio.TimeoutError):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_wrong_source_shape_and_old_sequence_do_not_admit(self):
        task = await self.start_wake()
        self.transport.wake(source=40002)
        self.transport.emit(b'\x0b\x1c' + bytes(4))
        self.transport.wake(sequence=9)
        await asyncio.sleep(.001)
        self.assertFalse(task.done())
        self.transport.wake()
        self.assertEqual(await task, self.now)
        self.assert_clean()

    async def test_wake_sequence_wrap_and_new_session(self):
        self.transport.sequence = 0xfffffe
        self.runtime.session()
        self.transport.native()
        task = await self.start_wake()
        self.transport.wake()
        self.assertEqual(await task, self.now)
        self.now += 1
        task = await self.start_wake()
        self.transport.wake()
        self.assertEqual(await task, self.now)
        self.assertEqual(self.editor.wake_sequences[40001][1], 1)
        self.runtime.session()
        self.transport.sequence = 20
        self.transport.native()
        task = await self.start_wake()
        self.transport.wake()
        self.assertEqual(await task, self.now)
        self.assert_clean()

    async def test_accessory_native_editor_does_not_wait_for_battery_wake(self):
        self.runtime.capabilities['dimmer_editor'] = True
        data = self.store.inventory()
        data['devices'][0]['product_id'] = 91
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        self.transport.native()
        result = await self.editor.assign(40001, 291, timeout=.2)
        self.assertTrue(result['acceptance_required'])
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.editor.wake_sequences, {})
        self.assert_clean()
