"""Synthetic managed dimmer transitions and private-target ownership."""
import asyncio
import copy
import json
import tempfile
import unittest

from avionmesh_pi.association import uuid_hash
from avionmesh_pi.dimmer import DimmerEditor
from avionmesh_pi.dimmer_control import dimmer_control_status, owned_dimmer_targets
from avionmesh_pi.errors import OperationNotStarted
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY


class Transport:
    ready = True
    dropped = 0
    def __init__(self):
        self.runtime = None
        self.frames = []
        self.sequence = 1
        self.native = True
        self.group = None
    def emit(self, payload, *, source=40001):
        self.runtime.observe(encrypt_mcp(b'\x00\x80\x73'+payload, KEY, self.sequence, source), self.runtime.clock())
        self.sequence += 1
    def command(self, group, level=255):
        product = self.runtime.core.devices[40001].product_id
        payload = b'\0\x0a'+group.to_bytes(2, 'big')
        payload += b'\x01\0'+bytes([level])+bytes(3) if product == 91 else b'\0'+bytes([level])+bytes(4)
        self.emit(payload)
    def wake(self):
        self.emit(b'\x0b\x1c'+bytes(5))
    async def send(self, frame):
        packet = decrypt_mcp(frame, KEY)
        self.frames.append(frame)
        self.group = int.from_bytes(packet.payload[5:7], 'big')
        assert packet.payload[:2] == b'\0\x1b'
        journal = self.runtime.store.journals()[-1]
        assert journal['status'] == 'attempted'
        assert journal['detail']['assignment_attempted'] is True
        if self.native:
            self.command(self.group)
    async def disconnect(self):
        self.ready = False


class DimmerControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 40001, 'product_id': 91, 'name': 'Synthetic dimmer', 'exposed': False},
            {'device_id': 40002, 'product_id': 93, 'name': 'Synthetic light A', 'exposed': False},
            {'device_id': 40003, 'product_id': 93, 'name': 'Synthetic light B', 'exposed': False}],
            'groups': [
                {'group_id': 260, 'name': 'Source', 'members': [40002], 'protected': False, 'exposed': False},
                {'group_id': 261, 'name': 'Destination', 'members': [40003], 'protected': False, 'exposed': False}]})
        self.claim = self.store.begin('claim', 40001, {'uuid': '01'*16, 'product_id': 91,
            'association_complete': True, 'uuid_hash': uuid_hash(bytes.fromhex('01'*16))})
        self.store.transition(self.claim, 'prepared', 'attempted')
        self.store.transition(self.claim, 'attempted', 'verified')
        self.transport = Transport()
        self.now = 10
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities = {'dimmer_editor': True, 'anyplace_editor': True, 'inventory_complete': True}
        self.runtime.session()
        self.editor = DimmerEditor(self.runtime)
        self.transport.command(260)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def anyplace(self):
        from avionmesh_pi.lifecycle import reload_inventory
        inventory = self.store.inventory()
        inventory['devices'][0]['product_id'] = 81
        self.store.set('inventory', inventory)
        claim = self.store.journals()[0]
        claim['detail']['product_id'] = 81
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(claim['detail']), self.claim))
        reload_inventory(self.runtime)
        self.transport.command(260)

    def options(self):
        return dict(retired_claims=self.store.get('retired_claims', []),
                    retired_addresses=self.store.get('retired_addresses', []),
                    target_sources=self.store.get('target_sources', {}))

    def status(self):
        return dimmer_control_status(40001, self.runtime.core.devices[40001].product_id,
                                     self.store.journals(), self.store.inventory()['groups'], **self.options())

    def submit(self, preview, *, timeout=1):
        return self.editor.configure(40001, preview['mode'],
            preview['group'] if preview['mode'] == 'native' else None,
            expected_baseline=preview['baseline'], expected_revision=preview['revision'], timeout=timeout)

    async def transition(self, mode, group=None, *, accept=True):
        preview = self.editor.preview_configuration(40001, mode, group)
        task = asyncio.create_task(self.submit(preview))
        await asyncio.sleep(0)
        if self.runtime.core.devices[40001].product_id == 81:
            self.transport.wake()
        result = await task
        if accept:
            accepted = self.editor.accept_observed(result['transaction'])
            self.assertTrue(accepted['accepted_observed_behavior'])
        return result

    async def test_preview_is_read_only_and_binds_reviewed_destination(self):
        state = (self.store.inventory(), self.store.journals(), self.store.get('dimmer_token'))
        preview = self.editor.preview_configuration(40001, 'native', 261)
        self.assertEqual(preview['source_mode'], 'native')
        self.assertEqual(preview['source_group'], 260)
        self.assertEqual(preview['valid_for_ms'], 15000)
        self.assertEqual(len(preview['revision']), 64)
        self.assertEqual((self.store.inventory(), self.store.journals(), self.store.get('dimmer_token')), state)
        self.assertEqual(self.transport.frames, [])

    async def test_native_to_native_requires_acceptance_and_preserves_sharing(self):
        result = await self.transition('native', 261, accept=False)
        self.assertEqual(len(self.transport.frames), 1)
        self.assertTrue(result['acceptance_required'])
        self.assertEqual(self.status()['state'], 'needs_review')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.editor.accept_observed(result['transaction'])
        self.assertEqual(self.status()['mode'], 'native')
        self.assertEqual(self.status()['group'], 261)
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assertFalse(self.status()['assignment_read_back'])

    async def test_native_ha_native_ha_reuses_owned_target_and_keeps_reservation(self):
        first = await self.transition('ha', accept=False)
        ha = first['group']
        self.assertEqual(ha, 24575)
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.editor.accept_observed(first['transaction'])
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(self.status()['mode'], 'ha')
        original_reservation = copy.deepcopy(next(g for g in self.store.inventory()['groups'] if g['group_id'] == ha))
        self.transport.command(ha)
        await self.transition('native', 260)
        self.assertEqual(self.status()['mode'], 'native')
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'], 'native mode preserves explicit sharing')
        self.assertIn(original_reservation, self.store.inventory()['groups'])
        self.transport.command(260)
        preview = self.editor.preview_configuration(40001, 'ha')
        self.assertTrue(preview['reuses_ha_target'])
        self.assertEqual(preview['group'], ha)
        result = await self.submit(preview)
        self.editor.accept_observed(result['transaction'])
        self.assertEqual(self.status()['mode'], 'ha')
        self.assertEqual(len([g for g in self.store.inventory()['groups'] if g['protected']]), 1)
        self.assertEqual(len(self.transport.frames), 3)

    async def test_anyplace_changes_wait_for_one_fresh_wake(self):
        self.anyplace()
        preview = self.editor.preview_configuration(40001, 'native', 261)
        task = asyncio.create_task(self.submit(preview))
        await asyncio.sleep(0)
        self.assertEqual(self.transport.frames, [])
        self.transport.wake()
        result = await task
        self.editor.accept_observed(result['transaction'])
        self.assertEqual(self.status()['mode'], 'native')
        self.assertEqual(len(self.transport.frames), 1)

    async def test_anyplace_wake_timeout_does_not_dispatch_and_keeps_burned_target(self):
        self.anyplace()
        preview = self.editor.preview_configuration(40001, 'ha')
        with self.assertRaises(OperationNotStarted):
            await self.submit(preview, timeout=.01)
        self.assertEqual(self.transport.frames, [])
        receipt = self.store.journals()[-1]
        self.assertEqual(receipt['status'], 'cancelled')
        self.assertTrue(receipt['detail']['dispatch_not_started'])
        self.assertIn(preview['group'], self.store.get('known_targets'))
        self.assertTrue(self.runtime.mesh_ready)
        new = self.editor.preview_configuration(40001, 'ha')
        self.assertNotEqual(new['group'], preview['group'])

    async def test_review_expiry_or_changed_inventory_refuses_before_journal(self):
        preview = self.editor.preview_configuration(40001, 'native', 261)
        inventory = self.store.inventory()
        inventory['groups'][1]['members'] = [40002]
        self.store.set('inventory', inventory)
        with self.assertRaises(OperationNotStarted):
            await self.submit(preview)
        self.assertEqual(len(self.store.journals()), 1)
        self.now += 15001
        with self.assertRaises(ValueError):
            self.editor.preview_configuration(40001, 'native', 261)
        self.assertEqual(self.transport.frames, [])

    async def test_wrong_baseline_revision_mode_and_private_destination_are_rejected(self):
        preview = self.editor.preview_configuration(40001, 'native', 261)
        with self.assertRaises(OperationNotStarted):
            await self.editor.configure(40001, 'native', 261, expected_baseline={}, expected_revision=preview['revision'])
        with self.assertRaises(OperationNotStarted):
            await self.editor.configure(40001, 'native', 261, expected_baseline=preview['baseline'], expected_revision='old')
        for mode, group in (('ha', 261), ('other', None), ('native', True), ('native', 260)):
            with self.assertRaises(ValueError):
                self.editor.preview_configuration(40001, mode, group)
        inventory = self.store.inventory()
        inventory['groups'][1].update(protected=True, members=[])
        self.store.set('inventory', inventory)
        with self.assertRaises(ValueError):
            self.editor.preview_configuration(40001, 'native', 261)
        self.assertEqual(self.transport.frames, [])

    async def test_foreign_protected_source_cannot_be_retargeted(self):
        inventory = self.store.inventory()
        inventory['groups'][0].update(protected=True, members=[])
        self.store.set('inventory', inventory)
        with self.assertRaisesRegex(ValueError, 'Protected source'):
            self.editor.preview_configuration(40001, 'native', 261)
        self.assertEqual(self.transport.frames, [])

    async def test_retired_or_duplicate_claim_and_disabled_flags_refuse(self):
        self.runtime.capabilities['inventory_complete'] = False
        with self.assertRaises(ValueError):
            self.editor.preview_configuration(40001, 'native', 261)
        self.runtime.capabilities['inventory_complete'] = True
        self.store.set('retired_addresses', [40001])
        with self.assertRaises(ValueError):
            self.editor.preview_configuration(40001, 'native', 261)
        self.store.set('retired_addresses', [])
        identifier = self.store.begin('claim', 40001, self.store.journals()[0]['detail'])
        self.store.transition(identifier, 'prepared', 'attempted')
        self.store.transition(identifier, 'attempted', 'verified')
        with self.assertRaises(ValueError):
            self.editor.preview_configuration(40001, 'native', 261)
        self.assertEqual(self.transport.frames, [])

    async def test_packet_ack_or_silence_never_proves_assignment_and_recovery_does_not_resend(self):
        self.transport.native = False
        preview = self.editor.preview_configuration(40001, 'native', 261)
        with self.assertRaises(asyncio.TimeoutError) as failure:
            await self.submit(preview, timeout=.01)
        receipt = self.store.journals()[-1]
        self.assertEqual(failure.exception.operation_result,
            {'transaction': receipt['id'], 'mode': 'native', 'group': 261})
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertFalse(self.runtime.mesh_ready)
        self.runtime.mesh_ready = self.transport.ready = True
        self.runtime.session()
        task = asyncio.create_task(self.editor.reconcile(receipt['id'], timeout=1))
        await asyncio.sleep(0)
        self.transport.command(261)
        result = await task
        self.assertTrue(result['write_retried'] is False)
        self.editor.accept_observed(receipt['id'])
        self.assertEqual(self.status()['group'], 261)
        self.assertEqual(len(self.transport.frames), 1)

    async def test_anyplace_recovery_requires_new_wake_and_native_observation(self):
        self.anyplace()
        self.transport.native = False
        preview = self.editor.preview_configuration(40001, 'native', 261)
        task = asyncio.create_task(self.submit(preview, timeout=.01))
        await asyncio.sleep(0)
        self.transport.wake()
        with self.assertRaises(asyncio.TimeoutError):
            await task
        receipt = self.store.journals()[-1]
        self.runtime.mesh_ready = self.transport.ready = True
        self.runtime.session()
        task = asyncio.create_task(self.editor.reconcile(receipt['id'], timeout=1))
        await asyncio.sleep(0)
        self.transport.wake()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.transport.command(261)
        result = await task
        self.assertFalse(result['write_retried'])
        self.editor.accept_observed(receipt['id'])
        self.assertEqual(len(self.transport.frames), 1)

    async def test_changed_claim_after_sequence_storage_prevents_radio_dispatch(self):
        preview = self.editor.preview_configuration(40001, 'native', 261)
        allocate = self.store.allocate_sequence
        def changed():
            value = allocate()
            self.store.set('retired_claims', [self.claim])
            return value
        self.store.allocate_sequence = changed
        with self.assertRaises(OperationNotStarted):
            await self.submit(preview)
        self.assertEqual(self.transport.frames, [])
        self.assertTrue(self.store.journals()[-1]['detail']['dispatch_not_started'])

    async def test_changed_private_ownership_before_radio_is_not_overwritten(self):
        preview = self.editor.preview_configuration(40001, 'ha')
        allocate = self.store.allocate_sequence
        def changed():
            value = allocate()
            self.store.set('target_sources', {str(preview['group']): [40099]})
            return value
        self.store.allocate_sequence = changed
        with self.assertRaises(OperationNotStarted):
            await self.submit(preview)
        self.assertEqual(self.transport.frames, [])

    async def test_conflicting_native_command_even_after_desired_prevents_acceptance(self):
        send = self.transport.send
        async def contradictory(frame):
            await send(frame)
            self.transport.command(0)
        self.transport.send = contradictory
        preview = self.editor.preview_configuration(40001, 'native', 261)
        with self.assertRaises(RuntimeError):
            await self.submit(preview)
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)

    async def test_cancel_after_dispatch_remains_uncertain_and_cleans_waiters(self):
        self.transport.native = False
        preview = self.editor.preview_configuration(40001, 'ha')
        task = asyncio.create_task(self.submit(preview))
        for _ in range(10):
            if self.transport.frames:
                break
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertFalse(self.runtime.mesh_ready)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.runtime.packet_observers, [])
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(len(self.transport.frames), 1)

    async def test_late_changed_behavior_or_destination_cannot_be_accepted(self):
        result = await self.transition('native', 261, accept=False)
        self.transport.command(260)
        with self.assertRaises(ValueError):
            self.editor.accept_observed(result['transaction'])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')

    async def test_reused_target_requires_exclusive_source_and_retained_keypad_references(self):
        first = await self.transition('ha')
        ha = first['group']
        self.transport.command(ha)
        await self.transition('native', 260)
        self.transport.command(260)
        self.store.set('target_sources', {str(ha): [40099]})
        preview = self.editor.preview_configuration(40001, 'ha')
        self.assertFalse(preview['reuses_ha_target'])
        self.assertNotEqual(preview['group'], ha)
        self.store.set('target_sources', {str(ha): [40001]})
        self.store.db.execute('INSERT INTO bindings VALUES (?,?,0)', (40099, json.dumps({'slots': [{'target': ha, 'slot': 1}]})))
        preview = self.editor.preview_configuration(40001, 'ha')
        self.assertFalse(preview['reuses_ha_target'])
        self.assertNotEqual(preview['group'], ha)

    async def test_legacy_verified_ha_setup_can_switch_native_and_reuse_target(self):
        inventory = self.store.inventory()
        reservation = {'group_id': 24575, 'name': 'Private dimmer', 'members': [], 'protected': True, 'exposed': False}
        inventory['groups'].append(reservation)
        self.store.set('inventory', inventory)
        self.store.reserve_targets([24575])
        receipt = self.store.begin('accessory_setup', 40001, {'claim': self.claim,
            'reservation': copy.deepcopy(reservation), 'assignment_attempted': True,
            'baseline': {'sequence': 0},
            'observed': {'sequence': 1, 'payload': '000a5fff0100ff000000'}})
        self.store.transition(receipt, 'prepared', 'attempted')
        self.store.transition(receipt, 'attempted', 'verified')
        self.transport.command(24575)
        self.assertEqual(self.status()['mode'], 'ha')
        await self.transition('native', 260)
        self.transport.command(260)
        preview = self.editor.preview_configuration(40001, 'ha')
        self.assertEqual(preview['group'], 24575)
        self.assertTrue(preview['reuses_ha_target'])

    async def test_post_commit_publication_failure_preserves_accepted_mode(self):
        from unittest.mock import patch
        result = await self.transition('ha', accept=False)
        with patch('avionmesh_pi.dimmer_control.reload_inventory', side_effect=RuntimeError('Synthetic refresh failure')):
            accepted = self.editor.accept_observed(result['transaction'])
        self.assertTrue(accepted['publication_refresh_pending'])
        self.assertEqual(self.store.journals()[-1]['status'], 'verified')
        self.assertEqual(self.status()['mode'], 'ha')
        self.assertEqual(len(self.transport.frames), 1)

    async def test_anyplace_wake_expiring_during_sequence_storage_never_dispatches(self):
        self.anyplace()
        preview = self.editor.preview_configuration(40001, 'native', 261)
        allocate = self.store.allocate_sequence
        def delayed():
            value = allocate()
            self.now += 251
            return value
        self.store.allocate_sequence = delayed
        task = asyncio.create_task(self.submit(preview))
        await asyncio.sleep(0)
        self.transport.wake()
        with self.assertRaises(OperationNotStarted):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assertTrue(self.store.journals()[-1]['detail']['dispatch_not_started'])
        self.assertEqual(self.store.journals()[-1]['status'], 'cancelled')

    async def test_old_pre_wake_behavior_cannot_confirm_anyplace_retarget(self):
        self.anyplace()
        self.transport.native = False
        preview = self.editor.preview_configuration(40001, 'native', 261)
        send = self.transport.send
        async def stale(frame):
            await send(frame)
            self.transport.sequence = 1
            self.transport.command(261)
        self.transport.send = stale
        task = asyncio.create_task(self.submit(preview, timeout=.01))
        await asyncio.sleep(0)
        self.transport.wake()
        with self.assertRaises(asyncio.TimeoutError):
            await task
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)

    async def test_restart_after_observation_requires_fresh_reconciliation_not_acceptance(self):
        result = await self.transition('ha', accept=False)
        self.runtime.session()
        with self.assertRaises(ValueError):
            self.editor.accept_observed(result['transaction'])
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        task = asyncio.create_task(self.editor.reconcile(result['transaction'], timeout=1))
        await asyncio.sleep(0)
        self.transport.command(result['group'])
        await task
        self.editor.accept_observed(result['transaction'])
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(len(self.transport.frames), 1)

    async def test_foreign_reference_after_observation_blocks_acceptance(self):
        result = await self.transition('ha', accept=False)
        self.store.set('target_sources', {str(result['group']): [40001, 40099]})
        with self.assertRaises(ValueError):
            self.editor.accept_observed(result['transaction'])
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')

    async def test_history_or_group_change_while_recovery_waits_prevents_acceptance(self):
        self.transport.native = False
        preview = self.editor.preview_configuration(40001, 'native', 261)
        with self.assertRaises(asyncio.TimeoutError):
            await self.submit(preview, timeout=.01)
        receipt = self.store.journals()[-1]
        self.runtime.mesh_ready = self.transport.ready = True
        self.runtime.session()
        task = asyncio.create_task(self.editor.reconcile(receipt['id'], timeout=1))
        await asyncio.sleep(0)
        data = self.store.inventory()
        data['groups'][1]['members'] = [40002]
        self.store.set('inventory', data)
        self.transport.command(261)
        with self.assertRaises(ValueError):
            await task
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)

    async def test_private_group_name_is_never_ownership_proof(self):
        data = self.store.inventory()
        data['groups'].append({'group_id': 24575, 'name': 'HA dimmer target: 40001',
                              'members': [], 'protected': True, 'exposed': False})
        self.store.set('inventory', data)
        self.store.reserve_targets([24575])
        preview = self.editor.preview_configuration(40001, 'ha')
        self.assertEqual(preview['group'], 24574)
        self.assertFalse(preview['reuses_ha_target'])

    async def test_post_review_busy_failure_is_definite_no_start(self):
        preview = self.editor.preview_configuration(40001, 'native', 261)
        async with self.runtime.operation:
            with self.assertRaises(OperationNotStarted) as caught:
                await self.submit(preview)
        self.assertEqual(caught.exception.code, 'mesh_busy')
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(len(self.store.journals()), 1)

    async def test_out_of_range_command_after_observation_prevents_acceptance(self):
        result = await self.transition('native', 261, accept=False)
        self.transport.command(0)
        with self.assertRaises(ValueError):
            self.editor.accept_observed(result['transaction'])
        self.assertEqual(self.store.journals()[-1]['status'], 'uncertain')

    async def test_new_failed_reconciliation_invalidates_prior_acceptance_evidence(self):
        result = await self.transition('native', 261, accept=False)
        with self.assertRaises(asyncio.TimeoutError):
            await self.editor.reconcile(result['transaction'], timeout=.01)
        with self.assertRaises(ValueError):
            self.editor.accept_observed(result['transaction'])
        self.assertEqual(len(self.transport.frames), 1)

    async def test_initial_native_status_requires_forward_behavior_from_ping(self):
        claim = self.store.journals()[0]
        destination = self.store.inventory()['groups'][0]
        receipt = self.store.begin('accessory_native_setup', 40001, {
            'claim': self.claim, 'claim_receipt': claim, 'product_id': 91, 'mode': 'native',
            'reservation': destination, 'assignment_attempted': True, 'assignment_read_back': False,
            'baseline': {'sequence': 1}, 'observed': {'sequence': 2, 'payload': '000a01040100ff000000'}})
        self.store.transition(receipt, 'prepared', 'attempted')
        self.store.transition(receipt, 'attempted', 'verified')
        self.assertEqual(self.status()['mode'], 'native')
        detail = self.store.journals()[-1]['detail']
        detail['observed']['sequence'] = 1
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), receipt))
        self.assertEqual(self.status()['state'], 'needs_review')

    async def test_initial_anyplace_ha_status_requires_forward_behavior_from_wake(self):
        self.anyplace()
        data = self.store.inventory()
        group = {'group_id': 24575, 'name': 'Synthetic private Anyplace', 'members': [],
                 'exposed': False, 'protected': True}
        data['groups'].append(group)
        self.store.set('inventory', data)
        claim = self.store.journals()[0]
        detail = {'claim': self.claim, 'claim_receipt': claim, 'product_id': 81,
                  'reservation': group, 'assignment_attempted': True, 'wake': {'sequence': 1},
                  'observed': {'sequence': 2, 'payload': '000a5fff00ff00000000'}}
        receipt = self.store.begin('anyplace_setup', 40001, detail)
        self.store.transition(receipt, 'prepared', 'attempted')
        self.store.transition(receipt, 'attempted', 'verified')
        self.assertEqual(self.status()['mode'], 'ha')
        detail['observed']['sequence'] = 1
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), receipt))
        self.assertEqual(self.status()['state'], 'needs_review')

    async def test_unrelated_retained_keypad_tables_survive_json_roundtrip(self):
        self.store.db.execute('INSERT INTO bindings VALUES (?,?,0)',
            (40099, json.dumps({'slots': [{'target': 555, 'slot': 1}]})))
        self.store.db.execute('INSERT INTO bindings VALUES (?,?,1)',
            (40098, json.dumps({'slots': [{'target': 556, 'slot': 2}]})))
        first = await self.transition('ha')
        receipt = self.store.journals()[-1]
        self.assertEqual(set(receipt['detail']['bindings']), {'40099', '40098'})
        self.assertEqual(self.status()['mode'], 'ha')
        self.transport.command(first['group'])
        await self.transition('native', 261)
        self.assertEqual(self.status()['mode'], 'native')
        self.assertEqual(len(self.transport.frames), 2)

    async def test_repeated_mode_switches_keep_history_snapshots_linear(self):
        sizes = []
        group = 260
        for index in range(10):
            self.transport.command(group)
            result = await self.transition('ha' if index % 2 == 0 else 'native',
                                           None if index % 2 == 0 else 260)
            group = result['group']
            receipt = self.store.journals()[-1]
            self.assertTrue(all(set(item) == {'id', 'kind', 'status', 'detail_sha256'}
                                for item in receipt['detail']['prior']))
            sizes.append(len(json.dumps(receipt)))
        self.assertLess(sizes[-1], 6000)
        self.assertLess(sizes[-1], sizes[0]*3)
        self.assertLess(max(b-a for a,b in zip(sizes, sizes[1:])), 400)
        self.assertEqual(len(self.transport.frames), 10)
        self.assertEqual(len([g for g in self.store.inventory()['groups'] if g['protected']]), 1)
