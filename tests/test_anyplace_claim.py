"""Synthetic model81 claim proof: one fresh wake and Ping, never association replay."""
import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.association import envelope, open_envelope, uuid_hash
from avionmesh_pi.commissioning import Commissioning
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_association import IDENTITY, KEY, Peer


class AnyplaceClaimTransport:
    ready = True
    dropped = 0

    def __init__(self):
        self.runtime = None
        self.peer = Peer()
        self.frames = []
        self.associations = []
        self.pings = []
        self.sequence = 100
        self.reply = 'valid'
        self.on_ping = None

    def emit(self, payload, *, source=None, sequence=None, received=None):
        if source is None:
            source = self.peer.target or 40001
        if sequence is None:
            sequence = self.sequence
            self.sequence += 1
        frame = encrypt_mcp(b'\0\x80\x73' + payload, KEY, sequence, source)
        self.runtime.observe(frame, self.runtime.clock() if received is None else received)
        return frame

    def wake(self, **kwargs):
        return self.emit(b'\x0b\x1c' + bytes(5), **kwargs)

    async def send(self, frame):
        self.frames.append(frame)
        try:
            payload = open_envelope(frame)
        except ValueError:
            command = decrypt_mcp(frame, KEY)
            assert command.payload[:2] == b'\x06\xff'
            self.pings.append(command)
            if self.on_ping:
                self.on_ping()
            token = command.payload[4]
            if self.reply == 'none':
                return
            source = command.mcp_address + (self.reply == 'wrong_source')
            if self.reply == 'wrong_nonce': token = token + 1
            reply = b'\x06\xff' + bytes([token]) + b'\x01\0\0\0'
            if self.reply == 'echo': reply = command.payload
            if self.reply == 'ack': reply = b'\x06\xff' + bytes([token, 0])
            self.emit(reply, source=source,
                      sequence=self.sequence - 2 if self.reply == 'old_sequence' else None)
        else:
            if payload[0] == 0:
                reply = envelope(b'\x01' + IDENTITY)
            else:
                self.associations.append(payload[0])
                reply = self.peer.respond(frame)
            self.runtime.observe(reply, self.runtime.clock())

    async def disconnect(self):
        self.ready = False


class AnyplaceClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        self.now = 100
        self.transport = AnyplaceClaimTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = True
        self.claims = Commissioning(self.runtime)
        exchange = self.runtime.exchange
        async def quick_exchange(plaintext, predicate, timeout=5, **kwargs):
            return await exchange(plaintext, predicate, timeout=.02, **kwargs)
        self.runtime.exchange = quick_exchange

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def checkpoint(self):
        identifier = self.store.begin('claim', 40001, {'uuid': IDENTITY.hex(),
            'uuid_hash': uuid_hash(IDENTITY), 'product_id': 81, 'name': 'Synthetic Anyplace',
            'association_complete': True, 'inventory_complete': True,
            'submissions_attempted': 12, 'submissions_accepted': 12})
        self.store.transition(identifier, 'prepared', 'attempted')
        self.store.transition(identifier, 'attempted', 'uncertain')
        return identifier

    def receipt(self, identifier):
        return next(j for j in self.store.journals() if j['id'] == identifier)

    def mutate(self, identifier, **detail):
        value = self.receipt(identifier)['detail']
        value.update(detail)
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(value), identifier))

    async def waiting(self, task):
        for _ in range(100):
            if any(p['phase'] == 'waiting_for_wake' for p in self.claims.progress.values()):
                return
            if task.done():
                await task
            await asyncio.sleep(0)
        self.fail('No wake prompt')

    async def start(self, identifier):
        task = asyncio.create_task(self.claims.reconcile(identifier))
        await asyncio.sleep(0)
        return task

    def assert_clean(self):
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.runtime.packet_observers, [])
        self.assertEqual(self.runtime.raw_observers, [])
        self.assertFalse(self.runtime.operation.locked())

    async def test_initial_claim_keeps_twelve_association_writes_then_waits_for_wake(self):
        await self.claims.discover(seconds=0)
        task = asyncio.create_task(self.claims.claim(IDENTITY.hex(), 'Synthetic Anyplace', 81,
                                                    inventory_complete=True))
        await self.waiting(task)
        self.assertEqual(len(self.transport.associations), 12)
        self.assertEqual(self.transport.pings, [])
        target = self.transport.peer.target
        self.assertEqual(self.claims.progress[target], {'phase': 'waiting_for_wake',
                                                       'uuid_hash': uuid_hash(IDENTITY)})
        self.assertFalse(self.store.inventory()['devices'])
        self.transport.wake()
        self.assertTrue((await task)['associated'])
        self.assertEqual(len(self.transport.associations), 12)
        self.assertEqual(len(self.transport.pings), 1)
        self.assertEqual(self.claims.progress[target]['phase'], 'verified')
        self.assertFalse(self.store.inventory()['devices'][0]['exposed'])
        self.assert_clean()

    async def test_initial_timeout_then_explicit_reconcile_never_replays_association(self):
        await self.claims.discover(seconds=0)
        with patch('avionmesh_pi.commissioning.ANYPLACE_WAKE_WAIT', .01):
            with self.assertRaises(asyncio.TimeoutError):
                await self.claims.claim(IDENTITY.hex(), 'Synthetic Anyplace', 81,
                                        inventory_complete=True)
        receipt, = self.store.journals()
        self.assertTrue(receipt['detail']['association_complete'])
        self.assertEqual(receipt['detail']['failure_code'], 'association_wake_timeout')
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertEqual(self.transport.pings, [])
        self.runtime.mesh_ready = self.transport.ready = True
        task = await self.start(receipt['id'])
        self.transport.wake()
        self.assertTrue((await task)['verified'])
        self.assertEqual(len(self.transport.associations), 12)
        self.assertEqual(len(self.transport.pings), 1)
        self.assert_clean()

    async def test_initial_nonce_ping_timeout_is_distinct_from_no_wake(self):
        await self.claims.discover(seconds=0)
        self.transport.reply = 'none'
        task = asyncio.create_task(self.claims.claim(IDENTITY.hex(), 'Synthetic Anyplace', 81,
                                                    inventory_complete=True))
        await self.waiting(task)
        self.transport.wake()
        with self.assertRaises(asyncio.TimeoutError):
            await task
        receipt, = self.store.journals()
        self.assertEqual(receipt['detail']['failure_code'], 'association_ping_timeout')
        self.assertEqual(len(self.transport.associations), 12)
        self.assertEqual(len(self.transport.pings), 1)
        self.assertFalse(self.store.inventory()['devices'])
        self.assert_clean()

    async def test_receive_age_250ms_boundary_still_allows_one_proof(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        self.transport.wake()
        self.now += 250
        self.assertTrue((await task)['verified'])
        self.assertEqual(len(self.transport.pings), 1)
        self.assert_clean()

    async def test_reconcile_requires_nonce_proof_not_wake_alone(self):
        identifier = self.checkpoint()
        original = copy.deepcopy(self.receipt(identifier))
        self.transport.reply = 'none'
        task = await self.start(identifier)
        self.transport.wake()
        result = await task
        self.assertFalse(result['verified'])
        self.assertEqual(len(self.transport.pings), 1)
        self.assertEqual(self.receipt(identifier), original)
        self.assertFalse(self.store.inventory()['devices'])
        self.assertEqual(self.claims.progress[40001]['phase'], 'needs_review')
        self.assert_clean()

    async def test_ack_echo_wrong_source_nonce_and_old_sequence_are_not_proof(self):
        identifier = self.checkpoint()
        for response in ('ack', 'echo', 'wrong_source', 'wrong_nonce', 'old_sequence'):
            with self.subTest(response=response):
                self.transport.reply = response
                task = await self.start(identifier)
                self.transport.wake()
                self.assertFalse((await task)['verified'])
                self.assertEqual(self.receipt(identifier)['status'], 'uncertain')
                self.assert_clean()
        self.assertEqual(len(self.transport.pings), 5)
        self.assertEqual(len({p.payload[4] for p in self.transport.pings}), 5)
        self.assertEqual(self.transport.associations, [])

    async def test_missing_complete_checkpoint_never_waits_or_transmits(self):
        identifier = self.checkpoint()
        self.mutate(identifier, association_complete=False)
        with self.assertRaises(ValueError):
            await self.claims.reconcile(identifier)
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_wrong_hash_checkpoint_never_waits_or_transmits(self):
        identifier = self.checkpoint()
        self.mutate(identifier, uuid_hash=123)
        with self.assertRaises(ValueError):
            await self.claims.reconcile(identifier)
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_delayed_task_cannot_renew_wake_timestamp(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        self.transport.wake()
        self.now += 251
        with self.assertRaisesRegex(RuntimeError, 'wake dispatch'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_later_wake_does_not_replace_first_receipt(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        self.transport.wake()
        self.now += 200
        self.transport.wake()
        self.now += 51
        with self.assertRaisesRegex(RuntimeError, 'wake dispatch'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_sequence_allocation_delay_cannot_dispatch_after_wake_budget(self):
        identifier = self.checkpoint()
        allocate = self.store.allocate_sequence
        def delayed():
            sequence = allocate()
            self.now += 251
            return sequence
        self.store.allocate_sequence = delayed
        task = await self.start(identifier)
        self.transport.wake()
        with self.assertRaisesRegex(RuntimeError, 'wake dispatch'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_token_allocation_delay_cannot_dispatch_after_wake_budget(self):
        from avionmesh_pi.identity import ping_token
        identifier = self.checkpoint()
        def delayed(store):
            token = ping_token(store)
            self.now += 251
            return token
        with patch('avionmesh_pi.commissioning.ping_token', side_effect=delayed):
            task = await self.start(identifier)
            self.transport.wake()
            with self.assertRaisesRegex(RuntimeError, 'wake dispatch'):
                await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_receipt_mutation_during_allocation_stops_before_ping(self):
        identifier = self.checkpoint()
        allocate = self.store.allocate_sequence
        def changed():
            sequence = allocate()
            self.mutate(identifier, name='Changed receipt')
            return sequence
        self.store.allocate_sequence = changed
        task = await self.start(identifier)
        self.transport.wake()
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            await task
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_receipt_mutation_before_reply_cannot_accept_claim(self):
        identifier = self.checkpoint()
        self.transport.on_ping = lambda: self.mutate(identifier, name='Changed after dispatch')
        task = await self.start(identifier)
        self.transport.wake()
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            await task
        self.assertEqual(len(self.transport.pings), 1)
        self.assertFalse(self.store.inventory()['devices'])
        self.assert_clean()

    async def test_session_change_while_waiting_aborts_immediately(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        self.runtime.session()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, .05)
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_cancelled_wake_is_consumed_after_duplicate_cache_expires(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        frame = self.transport.wake()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.now += 6000
        with patch('avionmesh_pi.commissioning.ANYPLACE_WAKE_WAIT', .01):
            task = await self.start(identifier)
            self.runtime.observe(frame, self.now)
            self.assertFalse((await task)['verified'])
        self.assertEqual(self.transport.frames, [])
        self.assert_clean()

    async def test_old_received_wrong_source_and_shape_wakes_are_ignored(self):
        identifier = self.checkpoint()
        task = await self.start(identifier)
        self.transport.wake(source=40002)
        self.transport.emit(b'\x0b\x1c' + bytes(4))
        self.transport.wake(received=self.now - 1)
        await asyncio.sleep(0)
        self.assertEqual(self.transport.frames, [])
        self.assertFalse(task.done())
        self.transport.wake()
        self.assertTrue((await task)['verified'])
        self.assertEqual(len(self.transport.pings), 1)
        self.assert_clean()

    async def test_automatic_recovery_requires_explicit_owner_wake(self):
        identifier = self.checkpoint()
        result = await self.claims.recover_safe_pending(identifier)
        self.assertEqual(result['outcome'], 'owner_wake_required')
        self.assertEqual(result['ping_attempts'], 0)
        self.assertTrue(result['requires_review'])
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.claims.progress, {})
        self.assert_clean()
