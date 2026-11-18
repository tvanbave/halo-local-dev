"""Synthetic model-126 HA/native switching through real wake admission.

These are protocol/workflow tests, not physical battery-keypad qualification.
"""
import asyncio
import copy
import tempfile
import unittest

from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.keypad_ownership import owned_targets
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_keypad_lost_reply import DATA, LossyTransport
from test_service import KEY


class BatteryManagedEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        data = copy.deepcopy(DATA)
        data['devices'][0]['product_id'] = 126
        self.store.import_inventory(data)
        self.now, self.wakes, self.writes_at = 10, [], []
        self.transport = LossyTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities = {'battery_editor': True, 'inventory_complete': True}
        self.runtime.session()
        exchange = self.runtime.exchange
        async def quick(command, predicate, timeout=3, **options):
            return await exchange(command, predicate, timeout=min(timeout, .05), **options)
        self.runtime.exchange = quick
        send = self.transport.send
        async def observe_write(frame):
            payload = decrypt_mcp(frame, KEY).payload
            if payload[0] in (2, 5):
                self.writes_at.append((self.now, self.wakes[-1]))
                receipt = self.store.journals()[-1]
                self.assertEqual(receipt['status'], 'attempted')
                group = receipt['detail']['managed'].get('new')
                if group:
                    self.assertIn(group, self.store.inventory()['groups'])
                    self.assertIn(group['group_id'], self.store.get('known_targets'))
            await send(frame)
        self.transport.send = observe_write
        self.editor = KeypadService(self.runtime, sleeper=self.sleep)
        self.baseline = await self.editor.read(40001)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def sleep(self, seconds):
        self.now += round(seconds * 1000)
        if self.editor.progress.get(40001) == 'settling':
            return
        def wake():
            self.wakes.append(self.now)
            self.runtime.observe(encrypt_mcp(b'\0\x80\x73\x0b\x1c' + bytes(5),
                KEY, self.transport.sequence, 40001), self.now)
            self.transport.sequence += 1
        asyncio.get_running_loop().call_soon(wake)

    async def save(self, changes):
        table = self.runtime.core.controllers.bindings[40001]
        return await self.editor.apply_many(40001, changes, table['revision'], allowed_models=(126,))

    def reconnect(self):
        self.transport.failed = False
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()

    def verbs(self):
        return [payload[0] for payload in self.transport.writes]

    async def test_native_to_ha_preserves_other_slots_and_uses_fresh_wakes(self):
        devices = copy.deepcopy(self.store.inventory()['devices'])
        result = await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertTrue(result['verified'])
        table = self.runtime.core.controllers.bindings[40001]
        self.assertEqual(table['slots'][1:], self.baseline['slots'][1:])
        self.assertEqual((table['slots'][0]['target'], table['slots'][0]['level']), (24575, 255))
        self.assertEqual(self.verbs(), [5, 2, 2])
        self.assertTrue(all(150 <= now - wake <= 1500 for now, wake in self.writes_at))
        self.assertTrue(all(second - first >= 20000 for first, second in zip(self.wakes, self.wakes[1:])))
        self.assertEqual(self.store.journals()[0]['detail']['product_id'], 126)
        self.assertIn(24575, owned_targets(self.store, 40001))
        self.assertEqual(self.store.inventory()['devices'], devices)

    async def test_ha_to_native_and_back_preserves_burned_reservations(self):
        await self.save([{'slot': 1, 'mode': 'ha'}])
        reservation = copy.deepcopy(self.store.inventory()['groups'][-1])
        await self.save([{'slot': 1, 'mode': 'native', 'group': 293, 'brightness': 128}])
        row = self.runtime.core.controllers.bindings[40001]['slots'][0]
        self.assertEqual((row['target'], row['level']), (293, 128))
        self.assertEqual(owned_targets(self.store, 40001), {})
        self.assertIn(reservation, self.store.inventory()['groups'])
        await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertIn(24574, owned_targets(self.store, 40001))
        self.assertTrue({24574, 24575}.issubset(self.store.get('known_targets')))

    async def test_wired_flag_does_not_enable_battery_ha_editing(self):
        self.runtime.capabilities = {'wired_editor': True, 'inventory_complete': True}
        wakes = len(self.wakes)
        with self.assertRaisesRegex(ValueError, 'model editor'):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertEqual(len(self.wakes), wakes)
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])

    async def test_stale_table_refuses_without_reservation_or_setter(self):
        self.transport.rows[0] = bytes.fromhex('0130000101230880')
        with self.assertRaisesRegex(ValueError, 'revision is stale'):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])
        self.assertFalse(any(g['group_id'] == 24575 for g in self.store.inventory()['groups']))

    async def test_expired_wake_never_dispatches_setter_and_read_recovery_never_retries(self):
        token = self.editor._token
        def late_token():
            result = token()
            self.now += 1501
            return result
        self.editor._token = late_token
        with self.assertRaisesRegex(RuntimeError, 'admission expired'):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        receipt = self.store.journals()[0]
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertEqual(self.transport.writes, [])
        self.assertIn(24575, self.store.get('known_targets'))
        self.reconnect()
        self.runtime.capabilities['battery_editor'] = False
        result = await self.editor.reconcile(receipt['id'])
        self.assertTrue(result['unchanged'])
        self.assertFalse(result['write_retried'])
        self.assertEqual(self.transport.writes, [])

    async def test_lost_primary_stops_save_and_recovery_never_resends(self):
        self.transport.silent = {'primary'}
        with self.assertRaises(ValueError):
            await self.save([{'slot': 2, 'mode': 'ha'}, {'slot': 3, 'mode': 'ha'}])
        receipt = self.store.journals()[0]
        self.assertEqual(self.verbs(), [2])
        self.assertEqual(self.editor.saves[40001]['buttons'][1]['state'], 'not_started')
        self.assertEqual(owned_targets(self.store, 40001), {})
        self.reconnect()
        result = await self.editor.reconcile(receipt['id'])
        self.assertTrue(result['unchanged'])
        self.assertEqual(self.verbs(), [2])
        self.assertIn(24575, self.store.get('known_targets'))

    async def test_delete_checkpoint_resume_requires_battery_editor_and_never_repeats_delete(self):
        token, calls = self.editor._token, 0
        def fail_second():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('synthetic interruption after verified deletion')
            return token()
        self.editor._token = fail_second
        with self.assertRaises(RuntimeError):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        receipt = self.store.journals()[0]
        self.assertEqual(receipt['detail']['stages'], ['delete'])
        self.editor._token = token
        self.reconnect()
        self.runtime.capabilities['battery_editor'] = False
        with self.assertRaisesRegex(ValueError, 'model editor'):
            await self.editor.resume_edit(receipt['id'], allowed_models=(126,))
        self.assertEqual(self.verbs(), [5])
        self.runtime.capabilities['battery_editor'] = True
        self.assertTrue((await self.editor.resume_edit(receipt['id'], allowed_models=(126,)))['verified'])
        self.assertEqual(self.verbs(), [5, 2, 2])
        self.assertIn(24575, owned_targets(self.store, 40001))

    async def test_disabling_capability_during_wake_wait_prevents_dispatch(self):
        prepare = self.editor.prepare_write
        async def disabled(*args, **kwargs):
            reply = await prepare(*args, **kwargs)
            self.runtime.capabilities['battery_editor'] = False
            return reply
        self.editor.prepare_write = disabled
        with self.assertRaisesRegex(ValueError, 'model editor'):
            await self.save([{'slot': 1, 'mode': 'ha'}])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_protected_assignment_requires_exact_owned_receipt(self):
        self.transport.rows[0] = bytes.fromhex('01300001012408ff')
        await self.editor.read(40001)
        for change in ({'slot': 1, 'mode': 'ha'},
                       {'slot': 1, 'mode': 'native', 'group': 291, 'brightness': 255},
                       {'slot': 1, 'mode': 'unassigned'}):
            with self.assertRaisesRegex(ValueError, 'managed lifecycle'):
                await self.save([change])
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(self.store.journals(), [])
