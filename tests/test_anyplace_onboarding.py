"""Synthetic model-81 enrollment boundaries; no hardware qualification."""
import asyncio
import copy
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.anyplace import AnyplaceOnboarding, cancelled_setups_allow_new
from avionmesh_pi.errors import OperationNotStarted
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY


class EnrollmentTransport:
    ready = True
    dropped = 0

    def __init__(self):
        self.runtime = None
        self.frames = []
        self.sequence = 10
        self.response = 'native'
        self.after_send = None
        self.disconnects = 0

    def emit(self, payload, *, sequence=None, source=40001, received=None):
        if sequence is None:
            sequence = self.sequence
            self.sequence = (self.sequence + 1) & 0xffffff or 1
        frame = encrypt_mcp(b'\0\x80\x73' + payload, KEY, sequence, source)
        self.runtime.observe(frame, self.runtime.clock() if received is None else received)
        return frame

    def wake(self, **options):
        return self.emit(b'\x0b\x1c' + bytes(5), **options)

    def native(self, group, level=128, **options):
        return self.emit(b'\0\x0a' + group.to_bytes(2, 'big') + bytes([0, level, 0, 0, 0, 0]),
                         **options)

    async def send(self, frame):
        packet = decrypt_mcp(frame, KEY)
        journal = self.runtime.store.journals()[-1]
        assert journal['kind'] == 'anyplace_setup'
        assert journal['status'] == 'attempted'
        assert journal['detail']['assignment_attempted'] is True
        assert packet.payload[:2] == b'\0\x1b'
        self.frames.append(frame)
        group = int.from_bytes(packet.payload[5:7], 'big')
        if self.response in ('native', 'desired_then_conflict'):
            self.native(group)
        if self.response in ('conflict', 'desired_then_conflict'):
            self.native(291)
        if self.response == 'ack':
            self.emit(b'\0\x1b' + bytes([packet.payload[4], 1]))
        if self.response == 'failure':
            raise ConnectionError('Synthetic send failure')
        if self.after_send:
            self.after_send()

    async def disconnect(self):
        self.disconnects += 1
        self.ready = False


class AnyplaceOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 40001, 'product_id': 81, 'name': 'Synthetic Anyplace', 'exposed': False}],
            'groups': [{'group_id': 291, 'name': 'Ordinary group', 'members': [],
                        'protected': False, 'exposed': False}]})
        self.claim = self.store.begin('claim', 40001,
            {'product_id': 81, 'uuid': '00112233445566778899aabbccddeeff', 'association_complete': True})
        self.store.transition(self.claim, 'prepared', 'attempted')
        self.store.transition(self.claim, 'attempted', 'verified')
        self.now = 100
        self.transport = EnrollmentTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities.update(anyplace_onboarding=True, inventory_complete=True)
        self.runtime.session()
        self.onboarding = AnyplaceOnboarding(self.runtime)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def start(self, timeout=.1):
        task = asyncio.create_task(self.onboarding.setup(40001, timeout=timeout))
        await asyncio.sleep(0)
        self.assertEqual(self.onboarding.progress[40001], 'waiting_for_wake')
        return task

    def journal(self):
        return self.store.journals()[-1]

    def assert_clean(self):
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.runtime.packet_observers, [])
        self.assertFalse(self.runtime.operation.locked())

    def assert_cancelled(self):
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.journal()['status'], 'cancelled')
        self.assertTrue(self.journal()['detail']['dispatch_not_started'])
        self.assertEqual(self.transport.disconnects, 0)
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assert_clean()

    async def cancelled(self):
        task = await self.start()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_cancelled()
        return copy.deepcopy(self.journal())

    async def uncertain(self, response='failure'):
        self.transport.response = response
        task = await self.start(timeout=.01)
        self.transport.wake()
        with self.assertRaises((ConnectionError, asyncio.TimeoutError, RuntimeError)):
            await task
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertTrue(self.journal()['detail']['assignment_attempted'])
        self.assertFalse(self.runtime.mesh_ready)
        self.assertEqual(self.transport.disconnects, 1)
        self.assert_clean()
        return self.journal()['id']

    def reconnect(self):
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()

    async def wait_observing(self):
        for _ in range(20):
            await asyncio.sleep(0)
            if self.onboarding.progress.get(40001) == 'observing':
                return
        self.fail('Synthetic observation listener did not start')

    async def test_fresh_claim_without_original_baseline_has_one_setter_and_observed_acceptance(self):
        task = await self.start()
        self.assertNotIn(40001, self.runtime.native_observations)
        self.assertEqual(self.journal()['status'], 'prepared')
        self.assertIn(self.journal()['detail']['reservation']['group_id'], self.store.get('known_targets'))
        self.transport.wake()
        result = await task
        self.assertEqual(result['transaction'], self.journal()['id'])
        self.assertTrue(result['ready'])
        self.assertTrue(result['native_behavior_observed'])
        self.assertFalse(result['assignment_read_back'])
        self.assertEqual(result['original_assignment'], 'unknown')
        self.assertEqual(self.journal()['detail']['product_id'], 81)
        self.assertEqual(self.journal()['detail']['claim'], self.claim)
        self.assertEqual(self.journal()['status'], 'verified')
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(decrypt_mcp(self.transport.frames[0], KEY).payload.hex(), '001b0000015fff010000')
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(self.onboarding.progress[40001], 'complete')
        self.assert_clean()

    async def test_first_wake_receipt_age_not_task_resumption_controls_dispatch(self):
        task = await self.start(timeout=1)
        self.transport.wake()
        self.now += 251
        with self.assertRaises(OperationNotStarted) as error:
            await task
        self.assertEqual(error.exception.code, 'anyplace_admission_expired')
        self.assertFalse(error.exception.response()['operation_started'])
        self.assertFalse(error.exception.response()['retry_allowed'])
        self.assertFalse(error.exception.response()['requires_review'])
        self.assert_cancelled()

    async def test_exact_250_ms_boundary_is_allowed(self):
        task = await self.start(timeout=1)
        self.transport.wake()
        self.now += 250
        self.assertTrue((await task)['ready'])
        self.assertEqual(len(self.transport.frames), 1)

    async def test_later_wake_does_not_extend_first_receipt(self):
        task = await self.start(timeout=1)
        self.transport.wake()
        self.now += 200
        self.transport.wake()
        self.now += 51
        with self.assertRaises(OperationNotStarted):
            await task
        self.assert_cancelled()

    async def test_sequence_allocation_delay_cancels_with_dispatch_not_started_proof(self):
        allocate = self.store.allocate_sequence
        def delayed():
            value = allocate()
            self.now += 251
            return value
        self.store.allocate_sequence = delayed
        task = await self.start(timeout=1)
        self.transport.wake()
        with self.assertRaises(OperationNotStarted):
            await task
        self.assert_cancelled()

    async def test_attempt_persistence_delay_is_rechecked_before_radio(self):
        save = self.onboarding.save
        def delayed(identifier, detail, status):
            save(identifier, detail, status)
            if status == 'attempted':
                self.now += 251
        self.onboarding.save = delayed
        task = await self.start(timeout=1)
        self.transport.wake()
        with self.assertRaises(OperationNotStarted):
            await task
        self.assertTrue(self.journal()['detail']['assignment_attempted'])
        self.assert_cancelled()

    async def test_wrong_source_shape_stale_receipt_and_old_sequence_cannot_wake(self):
        self.transport.native(291, sequence=20)
        self.transport.sequence = 21
        task = await self.start()
        self.transport.wake(source=40002)
        self.transport.emit(b'\x0b\x1c' + bytes(4))
        self.transport.wake(received=self.now - 1)
        self.transport.wake(sequence=20)
        await asyncio.sleep(0)
        self.assertEqual(self.transport.frames, [])
        self.assertFalse(task.done())
        self.transport.wake()
        self.assertTrue((await task)['ready'])

    async def test_sequence_wrap_is_supported_for_fresh_wake_and_behavior(self):
        self.transport.native(291, sequence=0xfffffe)
        self.transport.sequence = 0xffffff
        task = await self.start()
        self.transport.wake()
        self.assertTrue((await task)['ready'])
        self.assertEqual(self.journal()['detail']['observed']['sequence'], 1)

    async def test_cancelled_wait_retains_burned_target_and_requires_a_new_request(self):
        task = await self.start()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_cancelled()
        group = self.journal()['detail']['reservation']['group_id']
        self.assertIn(group, self.store.get('known_targets'))
        original = copy.deepcopy(self.journal())
        self.transport.wake()
        await asyncio.sleep(0)
        self.assertEqual(self.transport.frames, [])
        task = await self.start()
        self.assertNotEqual(self.journal()['detail']['reservation']['group_id'], group)
        self.assertGreater(self.journal()['detail']['token'], original['detail']['token'])
        self.transport.wake()
        self.assertTrue((await task)['ready'])
        self.assertEqual(self.onboarding.receipt(original['id']), original)
        self.assertEqual(len(self.transport.frames), 1)

    async def test_wake_timeout_allows_only_an_explicit_new_reviewed_request(self):
        task = await self.start(timeout=.01)
        with self.assertRaises(OperationNotStarted) as error:
            await task
        self.assertEqual(error.exception.code, 'anyplace_wake_timeout')
        self.assert_cancelled()
        original = copy.deepcopy(self.journal())
        self.transport.wake()
        await asyncio.sleep(0)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(len(self.store.journals()), 2)
        task = await self.start()
        self.transport.wake()
        self.assertTrue((await task)['ready'])
        self.assertEqual(len(self.transport.frames), 1)
        current = self.journal()
        self.assertNotEqual(current['detail']['reservation']['group_id'], original['detail']['reservation']['group_id'])
        self.assertNotEqual(current['detail']['token'], original['detail']['token'])
        self.assertEqual(self.onboarding.receipt(original['id']), original)

    async def test_durable_attempt_cancelled_before_radio_can_get_new_target_and_token(self):
        save = self.onboarding.save
        def delayed(identifier, detail, status):
            save(identifier, detail, status)
            if status == 'attempted':
                self.now += 251
        self.onboarding.save = delayed
        task = await self.start(timeout=1)
        self.transport.wake()
        with self.assertRaises(OperationNotStarted):
            await task
        self.assert_cancelled()
        original = copy.deepcopy(self.journal())
        self.assertTrue(original['detail']['assignment_attempted'])
        self.onboarding.save = save
        task = await self.start()
        self.transport.wake()
        self.assertTrue((await task)['ready'])
        self.assertEqual(len(self.transport.frames), 1)
        self.assertNotEqual(self.journal()['detail']['reservation']['group_id'], original['detail']['reservation']['group_id'])
        self.assertGreater(self.journal()['detail']['token'], original['detail']['token'])
        self.assertEqual(self.onboarding.receipt(original['id']), original)

    async def test_mutated_or_foreign_cancel_proof_refuses_new_setup(self):
        original = await self.cancelled()
        pristine_inventory = self.store.inventory()
        known = self.store.get('known_targets')
        claim = self.onboarding.receipt(self.claim)
        defects = ('missing_proof', 'false_proof', 'foreign_claim', 'changed_claim_snapshot',
                   'missing_claim_snapshot', 'wrong_model', 'prior_observation', 'unburned_group',
                   'changed_group', 'foreign_source', 'owner_source', 'bad_token', 'future_token',
                   'retired_claim', 'current_claim_changed')
        for defect in defects:
            with self.subTest(defect=defect):
                detail = copy.deepcopy(original['detail'])
                self.store.set('inventory', pristine_inventory)
                self.store.set('known_targets', known)
                self.store.set('target_sources', {})
                self.store.set('retired_claims', [])
                self.onboarding.save(self.claim, claim['detail'], 'verified')
                if defect == 'missing_proof': detail.pop('dispatch_not_started')
                if defect == 'false_proof': detail['dispatch_not_started'] = False
                if defect == 'foreign_claim': detail['claim'] = 'another_claim'
                if defect == 'changed_claim_snapshot': detail['claim_receipt']['detail']['product_id'] = 91
                if defect == 'missing_claim_snapshot': detail.pop('claim_receipt')
                if defect == 'wrong_model': detail['product_id'] = 91
                if defect == 'prior_observation': detail['observed'] = {'sequence': 40}
                if defect == 'unburned_group': self.store.set('known_targets', [291])
                if defect == 'changed_group':
                    inventory = copy.deepcopy(pristine_inventory)
                    inventory['groups'][-1]['name'] = 'Changed group'
                    self.store.set('inventory', inventory)
                if defect in ('foreign_source', 'owner_source'):
                    self.store.observe_target(detail['reservation']['group_id'],
                                              40002 if defect == 'foreign_source' else 40001)
                if defect == 'bad_token': detail['token'] = 0
                if defect == 'future_token': detail['token'] = 255
                if defect == 'retired_claim': self.store.set('retired_claims', [self.claim])
                if defect == 'current_claim_changed':
                    changed = copy.deepcopy(claim['detail']); changed['name'] = 'Independent claim edit'
                    self.onboarding.save(self.claim, changed, 'verified')
                self.onboarding.save(original['id'], detail, 'cancelled')
                with self.assertRaises(ValueError):
                    await self.onboarding.setup(40001)
                self.assertEqual(len(self.store.journals()), 2)
                self.assertEqual(self.transport.frames, [])

    async def test_actual_attempted_uncertain_and_verified_receipts_never_allow_new_setup(self):
        original = await self.cancelled()
        for status in ('attempted', 'uncertain', 'verified'):
            with self.subTest(status=status):
                self.onboarding.save(original['id'], original['detail'], status)
                with self.assertRaises((ValueError, RuntimeError)):
                    await self.onboarding.setup(40001)
                self.assertEqual(len(self.store.journals()), 2)
                self.assertEqual(self.transport.frames, [])

    async def test_changed_abandoned_receipt_while_new_request_waits_stops_dispatch(self):
        original = await self.cancelled()
        task = await self.start()
        altered = copy.deepcopy(original['detail'])
        altered['token'] = self.journal()['detail']['token']
        self.onboarding.save(original['id'], altered, 'cancelled')
        self.transport.wake()
        with self.assertRaises(OperationNotStarted):
            await task
        self.assert_cancelled()
        self.assertEqual(self.onboarding.receipt(original['id'])['detail'], altered)

    async def test_batch_cancel_proof_requires_unique_burned_targets_and_tokens(self):
        original = await self.cancelled()
        claim = self.onboarding.receipt(self.claim)
        options = {'target': 40001, 'claim': claim, 'groups': self.store.inventory()['groups'],
                   'known_targets': self.store.get('known_targets'), 'dimmer_token': self.store.get('dimmer_token')}
        self.assertTrue(cancelled_setups_allow_new([original], **options))
        duplicate = copy.deepcopy(original); duplicate['id'] = 'another_receipt'
        self.assertFalse(cancelled_setups_allow_new([original, duplicate], **options))
        self.assertFalse(cancelled_setups_allow_new([original], **dict(options, dimmer_token=0)))

    async def test_failed_cancellation_persistence_cannot_authorize_new_setup(self):
        save = self.onboarding.save
        def fail(identifier, detail, status):
            if status == 'cancelled':
                raise OSError('Synthetic cancellation save failure')
            save(identifier, detail, status)
        self.onboarding.save = fail
        task = await self.start(timeout=.01)
        with self.assertRaises(OSError):
            await task
        self.assertEqual(self.journal()['status'], 'prepared')
        self.assertNotIn('dispatch_not_started', self.journal()['detail'])
        with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
            await self.onboarding.setup(40001)
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_cancel_after_receipt_keeps_consumed_sequence(self):
        task = asyncio.create_task(self.onboarding._wake(40001, self.runtime.generation, self.now + 10000))
        await asyncio.sleep(0)
        frame = self.transport.wake()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.now += 6000
        task = asyncio.create_task(self.onboarding._wake(40001, self.runtime.generation, self.now + 10))
        await asyncio.sleep(0)
        self.runtime.observe(frame, self.now)
        with self.assertRaises(asyncio.TimeoutError):
            await task
        self.assert_clean()

    async def test_only_new_wake_after_admission_is_eligible(self):
        frame = self.transport.wake()
        task = await self.start(timeout=.01)
        self.runtime.observe(frame, self.now)
        with self.assertRaises(OperationNotStarted) as error:
            await task
        self.assertEqual(error.exception.code, 'anyplace_wake_timeout')
        self.assert_cancelled()

    async def test_session_change_before_radio_cancels(self):
        task = await self.start()
        self.transport.wake()
        self.runtime.session()
        with self.assertRaises(OperationNotStarted):
            await task
        self.assert_cancelled()

    async def test_identity_retirement_and_ownership_rechecked_after_sequence_storage(self):
        for mutate in ('retire_claim', 'change_device', 'change_group', 'foreign_owner', 'session', 'disable'):
            with self.subTest(mutate=mutate):
                # Each case owns a separate synthetic fixture and claim receipt.
                if mutate != 'retire_claim':
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                allocate = self.store.allocate_sequence
                def changed():
                    value = allocate()
                    if mutate == 'retire_claim':
                        self.store.set('retired_claims', [self.claim])
                    elif mutate == 'change_device':
                        data = self.store.inventory(); data['devices'][0]['product_id'] = 91
                        self.store.set('inventory', data)
                    elif mutate == 'change_group':
                        data = self.store.inventory(); data['groups'][-1]['exposed'] = True
                        self.store.set('inventory', data)
                    elif mutate == 'foreign_owner':
                        self.store.observe_target(self.journal()['detail']['reservation']['group_id'], 40002)
                    elif mutate == 'session':
                        self.runtime.session()
                    else:
                        self.runtime.capabilities['anyplace_onboarding'] = False
                    return value
                self.store.allocate_sequence = changed
                task = await self.start()
                self.transport.wake()
                with self.assertRaises((ValueError, RuntimeError)):
                    await task
                self.assert_cancelled()

    async def test_native_command_between_wake_and_dispatch_supersedes_wake(self):
        allocate = self.store.allocate_sequence
        def changed():
            value = allocate()
            self.transport.native(self.journal()['detail']['reservation']['group_id'])
            return value
        self.store.allocate_sequence = changed
        task = await self.start()
        self.transport.wake()
        with self.assertRaises(OperationNotStarted):
            await task
        self.assert_cancelled()

    async def test_ack_alone_remains_uncertain_and_unexposed(self):
        await self.uncertain('ack')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(len(self.transport.frames), 1)

    async def test_conflicting_native_target_even_after_desired_packet_rejects(self):
        await self.uncertain('desired_then_conflict')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])

    async def test_broadcast_or_non_group_native_target_after_desired_cannot_complete(self):
        for target in (0, 65535):
            with self.subTest(target=target):
                if target != 0:
                    await self.asyncTearDown()
                    await self.asyncSetUp()
                self.transport.after_send = lambda: self.transport.native(target, level=255)
                await self.uncertain('native')
                self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
                self.assertEqual(self.journal()['status'], 'uncertain')
                self.assertEqual(len(self.transport.frames), 1)

    async def test_latest_native_sequence_must_still_match_at_acceptance(self):
        save = self.onboarding.save
        def newer_native(identifier, detail, status):
            save(identifier, detail, status)
            if status == 'attempted' and 'observed' in detail:
                # The observation listener has finished; this non-group command
                # advances source order without replacing native_observations.
                self.transport.native(0, level=255)
        self.onboarding.save = newer_native
        task = await self.start()
        self.transport.wake()
        with self.assertRaisesRegex(ValueError, 'Latest Anyplace native behavior'):
            await task
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)
        self.assert_clean()

    async def test_pre_wake_native_sequence_cannot_complete_enrollment(self):
        self.transport.response = 'none'
        self.transport.after_send = lambda: self.transport.native(
            self.journal()['detail']['reservation']['group_id'], sequence=9)
        await self.uncertain('none')

    async def test_native_with_same_sequence_as_wake_cannot_complete_enrollment(self):
        self.transport.after_send = lambda: self.transport.native(
            self.journal()['detail']['reservation']['group_id'], sequence=10)
        await self.uncertain('none')

    async def test_changed_reservation_after_observation_prevents_acceptance(self):
        def changed():
            self.store.observe_target(self.journal()['detail']['reservation']['group_id'], 40002)
        self.transport.after_send = changed
        task = await self.start()
        self.transport.wake()
        with self.assertRaisesRegex(ValueError, 'ownership'):
            await task
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])

    async def test_post_commit_refresh_failure_preserves_acceptance_without_new_write(self):
        with patch('avionmesh_pi.anyplace.reload_inventory', side_effect=OSError('Synthetic refresh failure')):
            task = await self.start()
            self.transport.wake()
            result = await task
        self.assertFalse(result['ready'])
        self.assertTrue(result['publication_pending'])
        self.assertTrue(result['requires_review'])
        self.assertTrue(result['native_behavior_observed'])
        self.assertEqual(self.journal()['status'], 'verified')
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(self.transport.disconnects, 0)
        self.assertEqual(len(self.transport.frames), 1)
        with self.assertRaisesRegex(ValueError, 'cannot be retried'):
            await self.onboarding.setup(40001)
        self.assert_clean()

    async def test_post_commit_sink_failure_cannot_downgrade_verified_receipt(self):
        def failed_sink(message):
            raise RuntimeError('Synthetic publication failure')
        self.runtime.sink = failed_sink
        task = await self.start()
        self.transport.wake()
        result = await task
        self.assertFalse(result['ready'])
        self.assertTrue(result['publication_pending'])
        self.assertEqual(self.journal()['status'], 'verified')
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.assertEqual(self.transport.disconnects, 0)
        self.assertEqual(len(self.transport.frames), 1)
        self.assert_clean()

    async def test_delayed_transport_return_cannot_accept_expired_observation(self):
        self.transport.after_send = lambda: setattr(self, 'now', self.now + 101)
        task = await self.start()
        self.transport.wake()
        with self.assertRaisesRegex(asyncio.TimeoutError, 'deadline'):
            await task
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])

    async def test_cancel_after_dispatch_never_replays_and_disconnects(self):
        self.transport.response = 'none'
        task = await self.start()
        self.transport.wake()
        await self.wait_observing()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)
        self.assertFalse(self.runtime.mesh_ready)
        self.assert_clean()

    async def test_uncertain_reconciliation_uses_new_wake_and_observation_only(self):
        identifier = await self.uncertain()
        self.reconnect()
        task = asyncio.create_task(self.onboarding.reconcile(identifier, timeout=.1))
        await asyncio.sleep(0)
        self.transport.wake()
        await self.wait_observing()
        self.transport.native(self.journal()['detail']['reservation']['group_id'])
        result = await task
        self.assertTrue(result['ready'])
        self.assertFalse(result['write_retried'])
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.journal()['status'], 'verified')
        self.assertTrue(self.store.inventory()['devices'][0]['exposed'])
        self.assert_clean()

    async def test_reconciliation_rejects_original_wake_and_keeps_uncertain(self):
        identifier = await self.uncertain()
        self.reconnect()
        task = asyncio.create_task(self.onboarding.reconcile(identifier, timeout=.01))
        await asyncio.sleep(0)
        self.transport.wake(sequence=self.journal()['detail']['wake']['sequence'])
        with self.assertRaises(asyncio.TimeoutError):
            await task
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)
        self.assert_clean()

    async def test_reconciliation_requires_original_claim_and_safe_reservation(self):
        identifier = await self.uncertain()
        self.reconnect()
        self.store.set('retired_claims', [self.claim])
        with self.assertRaises(ValueError):
            await self.onboarding.reconcile(identifier)
        self.assertEqual(len(self.transport.frames), 1)

    async def test_reconciliation_never_changes_child_receipt_owned_by_external_reset(self):
        identifier = await self.uncertain()
        self.reconnect()
        original = self.journal()
        # The reset owner is inserted directly because it intentionally coexists
        # with the uncertain child that Store.begin ordinarily excludes.
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
            ('reset_owner', 'external_reset', 40001, 'prepared', '{}'))
        with self.assertRaisesRegex(ValueError, 'operation ownership'):
            await self.onboarding.reconcile(identifier)
        self.assertEqual(self.onboarding.receipt(identifier), original)
        self.assertEqual(len(self.transport.frames), 1)
        self.assert_clean()

    async def test_reconciliation_receipt_change_during_observation_is_not_overwritten(self):
        identifier = await self.uncertain()
        self.reconnect()
        task = asyncio.create_task(self.onboarding.reconcile(identifier, timeout=.1))
        await asyncio.sleep(0)
        self.transport.wake()
        await self.wait_observing()
        changed = self.journal()['detail']
        changed['review_marker'] = 'Synthetic independent update'
        self.onboarding.save(identifier, changed, 'uncertain')
        self.transport.native(changed['reservation']['group_id'])
        with self.assertRaisesRegex(ValueError, 'receipt or operation ownership'):
            await task
        self.assertEqual(self.journal()['detail'], changed)
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertEqual(len(self.transport.frames), 1)
        self.assert_clean()

    async def test_restart_never_replays_attempt_and_requires_new_observation(self):
        identifier = await self.uncertain()
        frames = list(self.transport.frames)
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.transport = EnrollmentTransport()
        self.transport.sequence = 20
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities.update(anyplace_onboarding=True, inventory_complete=True)
        self.runtime.session()
        self.onboarding = AnyplaceOnboarding(self.runtime)
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.journal()['status'], 'uncertain')
        with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
            await self.onboarding.setup(40001)
        task = asyncio.create_task(self.onboarding.reconcile(identifier, timeout=.1))
        await asyncio.sleep(0)
        self.transport.wake()
        await self.wait_observing()
        self.transport.native(self.journal()['detail']['reservation']['group_id'])
        self.assertTrue((await task)['ready'])
        self.assertEqual(len(frames), 1)
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_disconnect_failure_still_invalidates_failed_attempt_session(self):
        async def failure():
            raise ConnectionError('Synthetic disconnect failure')
        self.transport.disconnect = failure
        self.transport.response = 'failure'
        task = await self.start()
        self.transport.wake()
        generation = self.runtime.generation
        with self.assertRaisesRegex(ConnectionError, 'disconnect failure'):
            await task
        self.assertEqual(self.journal()['status'], 'uncertain')
        self.assertFalse(self.runtime.mesh_ready)
        self.assertGreater(self.runtime.generation, generation)
        self.assert_clean()

    async def test_reconciliation_cancel_keeps_receipt_and_never_transmits(self):
        identifier = await self.uncertain()
        self.reconnect()
        original = self.journal()
        task = asyncio.create_task(self.onboarding.reconcile(identifier, timeout=.1))
        await asyncio.sleep(0)
        self.transport.wake()
        await self.wait_observing()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.journal(), original)
        self.assertEqual(len(self.transport.frames), 1)
        self.assert_clean()

    async def test_disabled_capabilities_and_wrong_model_have_no_side_effects(self):
        for key in ('anyplace_onboarding', 'inventory_complete'):
            self.runtime.capabilities[key] = False
            with self.assertRaises(ValueError):
                await self.onboarding.setup(40001)
            self.runtime.capabilities[key] = True
        data = self.store.inventory(); data['devices'][0]['product_id'] = 91
        self.store.set('inventory', data)
        with self.assertRaises(ValueError):
            await self.onboarding.setup(40001)
        self.assertEqual(len(self.store.journals()), 1)
        self.assertEqual(self.transport.frames, [])

    async def test_claim_must_be_unique_same_model_verified_and_not_retired(self):
        duplicate = self.store.begin('claim', 40001, {'product_id': 81})
        self.store.transition(duplicate, 'prepared', 'attempted')
        self.store.transition(duplicate, 'attempted', 'verified')
        with self.assertRaisesRegex(ValueError, 'One verified retained'):
            await self.onboarding.setup(40001)
        self.assertEqual(self.transport.frames, [])

    async def test_prior_setup_or_edit_receipt_blocks_fresh_enrollment(self):
        for kind in ('anyplace_setup', 'accessory_setup', 'keypad_setup', 'dimmer_edit', 'keypad_edit'):
            identifier = self.store.begin(kind, 40001, {})
            self.store.transition(identifier, 'prepared', 'cancelled')
            with self.assertRaisesRegex(ValueError, 'cannot be retried'):
                await self.onboarding.setup(40001)
            self.store.db.execute('DELETE FROM journals WHERE id=?', (identifier,))
        self.assertEqual(self.transport.frames, [])

    async def test_unrelated_pending_operation_and_busy_lock_block_setup(self):
        await self.runtime.operation.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                await self.onboarding.setup(40001)
        finally:
            self.runtime.operation.release()
        self.store.begin('keypad_setup', 40002, {})
        with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
            await self.onboarding.setup(40001)
        self.assertEqual(self.transport.frames, [])

    async def test_token_never_wraps_and_reservations_skip_previously_used_targets(self):
        self.store.set('dimmer_token', 254)
        self.store.reserve_targets(range(24567, 24576))
        self.store.set('target_sources', {'24565': [40002]})
        task = await self.start()
        self.transport.wake()
        self.assertTrue((await task)['ready'])
        self.assertEqual(self.journal()['detail']['token'], 255)
        self.assertEqual(self.journal()['detail']['reservation']['group_id'], 24564)
        self.assertIn(24564, self.store.get('known_targets'))

    async def test_token_exhaustion_has_no_enrollment_or_radio_side_effects(self):
        self.store.set('dimmer_token', 255)
        with self.assertRaisesRegex(ValueError, 'token space'):
            await self.onboarding.setup(40001)
        self.assertEqual(len(self.store.journals()), 1)
        self.assertEqual(self.transport.frames, [])

    async def test_attempt_persistence_failure_has_no_radio_dispatch(self):
        save = self.onboarding.save
        def fail(identifier, detail, status):
            if status == 'attempted':
                raise OSError('Synthetic durable attempt failure')
            save(identifier, detail, status)
        self.onboarding.save = fail
        task = await self.start()
        self.transport.wake()
        with self.assertRaises(OperationNotStarted) as error:
            await task
        self.assertIsInstance(error.exception.__cause__, OSError)
        self.assert_cancelled()

    async def test_invalid_timeout_or_identifier_cannot_start_work(self):
        for timeout in (0, -1, 121, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                await self.onboarding.setup(40001, timeout=timeout)
        with self.assertRaises(ValueError):
            await self.onboarding.reconcile('missing')
        self.assertEqual(len(self.store.journals()), 1)
        self.assertEqual(self.transport.frames, [])
