"""Synthetic direct-group enrollment; no live controller or radio access."""
import asyncio
import copy
import json
import tempfile
import unittest

from avionmesh_pi.accessory import AccessoryOnboarding
from avionmesh_pi.association import uuid_hash
from avionmesh_pi.protocol import decrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_accessory import AccessoryTransport
from test_service import KEY


class NativeAccessoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 40001, 'product_id': 91, 'name': 'Synthetic dimmer', 'exposed': False},
            {'device_id': 40002, 'product_id': 93, 'name': 'Synthetic light', 'exposed': False}],
            'groups': [{'group_id': 260, 'name': 'Synthetic room', 'members': [40002],
                        'protected': False, 'exposed': False}]})
        self.claim = self.store.begin('claim', 40001, {'uuid': '01'*16, 'product_id': 91,
            'uuid_hash': uuid_hash(bytes.fromhex('01'*16)), 'association_complete': True})
        self.store.transition(self.claim, 'prepared', 'attempted')
        self.store.transition(self.claim, 'attempted', 'verified')
        self.transport = AccessoryTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities = {'accessory_onboarding': True, 'inventory_complete': True}
        self.runtime.session()
        self.setup = AccessoryOnboarding(self.runtime)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def change_claim(self, **updates):
        receipt = self.store.journals()[0]
        receipt['detail'].update(updates)
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                              (json.dumps(receipt['detail']), self.claim))

    def setters(self):
        return [decrypt_mcp(frame, KEY) for frame in self.transport.frames
                if decrypt_mcp(frame, KEY).payload[:2] == b'\0\x1b']

    async def test_native_setup_writes_once_to_lighting_group_without_ha_side_effects(self):
        inventory = copy.deepcopy(self.store.inventory())
        known = self.store.get('known_targets')
        result = await self.setup.setup_native(40001, 260)
        self.assertEqual(result['mode'], 'native')
        self.assertEqual(result['group'], 260)
        self.assertTrue(result['native_behavior_observed'])
        self.assertFalse(result['assignment_read_back'])
        self.assertEqual(self.store.inventory(), inventory)
        self.assertEqual(self.store.get('known_targets'), known)
        self.assertEqual(len(self.transport.frames), 2)
        self.assertEqual(len(self.setters()), 1)
        self.assertEqual(self.setters()[0].payload[5:7], b'\x01\x04')
        receipt = self.store.journals()[-1]
        self.assertEqual(receipt['kind'], 'accessory_native_setup')
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(receipt['detail']['claim_receipt'], self.store.journals()[0])

    async def test_native_and_ha_setup_cannot_overwrite_completed_native_setup(self):
        await self.setup.setup_native(40001, 260)
        for operation in (self.setup.setup_native(40001, 260), self.setup.setup(40001)):
            with self.assertRaises(ValueError):
                await operation
        self.assertEqual(len(self.setters()), 1)

    async def test_prior_ha_or_dimmer_edit_blocks_native_setup(self):
        for kind in ('accessory_setup', 'dimmer_edit'):
            with self.subTest(kind=kind):
                identifier = self.store.begin(kind, 40001, {})
                self.store.transition(identifier, 'prepared', 'attempted')
                self.store.transition(identifier, 'attempted', 'verified')
                with self.assertRaises(ValueError):
                    await self.setup.setup_native(40001, 260)
                self.store.db.execute('DELETE FROM journals WHERE id=?', (identifier,))
        self.assertEqual(self.transport.frames, [])

    async def test_exact_complete_claim_required_before_any_frame(self):
        original = self.store.journals()[0]['detail']
        for patch in ({'association_complete': False}, {'product_id': 81}, {'uuid': '00'*16},
                      {'uuid': 'xx'*16}, {'uuid': '01'*15}, {'uuid_hash': 0}):
            with self.subTest(patch=patch):
                self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                                      (json.dumps(original | patch), self.claim))
                with self.assertRaises(ValueError):
                    await self.setup.setup_native(40001, 260)
        self.assertEqual(self.transport.frames, [])

    async def test_ambiguous_or_retired_identity_blocks_native_setup(self):
        detail = self.store.journals()[0]['detail']
        for other_target in (40001, 40003):
            identifier = self.store.begin('claim', other_target, detail)
            self.store.transition(identifier, 'prepared', 'attempted')
            self.store.transition(identifier, 'attempted', 'verified')
            with self.assertRaises(ValueError):
                await self.setup.setup_native(40001, 260)
            self.store.db.execute('DELETE FROM journals WHERE id=?', (identifier,))
        for name, value in (('retired_claims', [self.claim]), ('retired_addresses', [40001])):
            self.store.set(name, value)
            with self.assertRaises(ValueError):
                await self.setup.setup_native(40001, 260)
            self.store.set(name, [])
        self.assertEqual(self.transport.frames, [])

    async def test_destination_must_be_ordinary_nonempty_known_lights(self):
        original = copy.deepcopy(self.store.inventory())
        for patch in ({'members': []}, {'protected': True}, {'members': [40001]},
                      {'members': [40099]}, {'members': [40002, 40002]}):
            with self.subTest(patch=patch):
                data = copy.deepcopy(original)
                data['groups'][0].update(patch)
                self.store.set('inventory', data)
                with self.assertRaises(ValueError):
                    await self.setup.setup_native(40001, 260)
        self.assertEqual(self.transport.frames, [])

    async def test_capability_offline_pending_and_timeout_admission(self):
        self.runtime.capabilities['accessory_onboarding'] = False
        with self.assertRaises(ValueError):
            await self.setup.setup_native(40001, 260)
        self.runtime.capabilities['accessory_onboarding'] = True
        self.transport.ready = False
        with self.assertRaises(RuntimeError):
            await self.setup.setup_native(40001, 260)
        self.transport.ready = True
        identifier = self.store.begin('group_membership', 40002, {})
        with self.assertRaises(RuntimeError):
            await self.setup.setup_native(40001, 260)
        self.store.transition(identifier, 'prepared', 'cancelled')
        for timeout in (0, -1, True, float('nan'), float('inf'), 121):
            with self.assertRaises(ValueError):
                await self.setup.setup_native(40001, 260, timeout=timeout)
        self.assertEqual(self.transport.frames, [])

    async def test_destination_changed_after_ping_blocks_setter(self):
        send = self.transport.send
        async def changed(frame):
            await send(frame)
            data = self.store.inventory()
            data['groups'][0]['members'] = []
            self.store.set('inventory', data)
        self.transport.send = changed
        with self.assertRaises(ValueError):
            await self.setup.setup_native(40001, 260)
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.setters(), [])
        self.assertFalse(self.transport.ready)

    async def test_claim_changed_after_sequence_allocation_blocks_setter(self):
        allocate = self.store.allocate_sequence
        count = 0
        def changed():
            nonlocal count
            count += 1
            result = allocate()
            if count == 2:
                self.change_claim(association_complete=False)
            return result
        self.store.allocate_sequence = changed
        with self.assertRaises(ValueError):
            await self.setup.setup_native(40001, 260)
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.setters(), [])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')

    async def test_ack_without_behavior_stays_uncertain_and_reconcile_never_rewrites(self):
        self.transport.native = False
        with self.assertRaises(asyncio.TimeoutError):
            await self.setup.setup_native(40001, 260, timeout=.01)
        receipt = self.store.journals()[-1]
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.runtime.mesh_ready = self.transport.ready = True
        self.runtime.session()
        task = asyncio.create_task(self.setup.reconcile(receipt['id'], timeout=1))
        await asyncio.sleep(0)
        self.transport.emit(b'\0\x0a\x01\x04\x01\0\xff\0\0\0')
        self.assertEqual((await task)['mode'], 'native')
        self.assertEqual(len(self.transport.frames), 2)
        self.assertEqual(len(self.setters()), 1)

    async def test_changed_claim_cannot_accept_uncertain_setup(self):
        self.transport.native = False
        with self.assertRaises(asyncio.TimeoutError):
            await self.setup.setup_native(40001, 260, timeout=.01)
        receipt = self.store.journals()[-1]
        self.runtime.mesh_ready = self.transport.ready = True
        self.runtime.session()
        task = asyncio.create_task(self.setup.reconcile(receipt['id'], timeout=1))
        await asyncio.sleep(0)
        self.change_claim(association_complete=False)
        self.transport.emit(b'\0\x0a\x01\x04\x01\0\xff\0\0\0')
        with self.assertRaises(ValueError):
            await task
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.setters()), 1)

    async def test_old_native_sequence_cannot_confirm_new_assignment(self):
        self.transport.native = False
        self.transport.sequence = 10
        send = self.transport.send
        async def stale(frame):
            await send(frame)
            if decrypt_mcp(frame, KEY).payload[:2] == b'\0\x1b':
                self.transport.sequence = 9
                self.transport.emit(b'\0\x0a\x01\x04\x01\0\xff\0\0\0')
        self.transport.send = stale
        with self.assertRaises(asyncio.TimeoutError):
            await self.setup.setup_native(40001, 260, timeout=.01)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')

    async def test_contradictory_behavior_cannot_confirm_assignment(self):
        send = self.transport.send
        async def contradictory(frame):
            await send(frame)
            if decrypt_mcp(frame, KEY).payload[:2] == b'\0\x1b':
                self.transport.emit(b'\0\x0a\x01\x05\x01\0\xff\0\0\0')
        self.transport.send = contradictory
        with self.assertRaises(RuntimeError):
            await self.setup.setup_native(40001, 260)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.setters()), 1)

    async def test_cancel_after_dispatch_never_retries_assignment(self):
        self.transport.native = False
        task = asyncio.create_task(self.setup.setup_native(40001, 260))
        for _ in range(10):
            if self.setters():
                break
            await asyncio.sleep(0)
        self.assertEqual(len(self.setters()), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertFalse(self.runtime.mesh_ready)
        self.assertFalse(self.transport.ready)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.runtime.packet_observers, [])

    async def test_disconnect_failure_still_invalidates_session(self):
        self.transport.native = False
        generation = self.runtime.generation
        async def failed_disconnect():
            raise RuntimeError('Synthetic disconnect failure')
        self.transport.disconnect = failed_disconnect
        with self.assertRaisesRegex(RuntimeError, 'Synthetic disconnect'):
            await self.setup.setup_native(40001, 260, timeout=.01)
        self.assertFalse(self.runtime.mesh_ready)
        self.assertGreater(self.runtime.generation, generation)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.setters()), 1)

    async def test_post_commit_publication_failure_preserves_verified_receipt(self):
        emit = self.runtime.emit
        def failed_emit(messages):
            if self.store.journals()[-1]['status'] == 'verified':
                raise RuntimeError('Synthetic publication failure')
            emit(messages)
        self.runtime.emit = failed_emit
        result = await self.setup.setup_native(40001, 260)
        self.assertTrue(result['ready'])
        self.assertTrue(result['publication_refresh_pending'])
        self.assertEqual(self.store.journals()[-1]['status'], 'verified')
        self.assertTrue(self.runtime.mesh_ready)
        self.assertEqual(len(self.setters()), 1)

    async def test_delayed_transport_return_cannot_accept_stale_behavior(self):
        now = [10]
        self.runtime.clock = lambda: now[0]
        send = self.transport.send
        async def delayed(frame):
            await send(frame)
            if decrypt_mcp(frame, KEY).payload[:2] == b'\0\x1b':
                now[0] += 15001
        self.transport.send = delayed
        with self.assertRaisesRegex(ValueError, 'Fresh current-session'):
            await self.setup.setup_native(40001, 260)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.setters()), 1)

    async def test_broadcast_after_desired_command_contradicts_target(self):
        send = self.transport.send
        async def contradictory(frame):
            await send(frame)
            if decrypt_mcp(frame, KEY).payload[:2] == b'\0\x1b':
                self.transport.emit(b'\0\x0a\xff\xff\x01\0\xff\0\0\0')
        self.transport.send = contradictory
        with self.assertRaises(RuntimeError):
            await self.setup.setup_native(40001, 260)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.setters()), 1)

