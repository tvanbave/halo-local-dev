"""Synthetic direct-first-device bootstrap; no Bluetooth or real mesh state.

The sole peer is both GATT endpoint and association target. Its disconnect
models key-install reboot, not evidence any physical RL4 actually reboots.
"""
import unittest
from types import SimpleNamespace

import test_association as base
from avionmesh_pi.association import envelope, open_envelope
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp


class BootstrapTransport:
    ready = True
    dropped = 0

    def __init__(self, runtime, disconnect_at, identity=base.IDENTITY):
        self.runtime = runtime
        self.identity = identity
        self.disconnect_at = disconnect_at
        self.peer = base.Peer()
        self.frames = []
        self.association_starts = 0
        self.final_ack_delivered = False
        self.pings = 0

    def boundary(self):
        self.ready = self.runtime.mesh_ready = False
        self.runtime.session()

    async def disconnect(self):
        self.ready = False

    async def send(self, frame):
        self.frames.append(frame)
        try:
            packet = open_envelope(frame)
        except ValueError:
            command = decrypt_mcp(frame, base.KEY)
            self.pings += 1
            if self.disconnect_at == 'after_checkpoint':
                self.disconnect_at = None
                self.boundary()
                raise RuntimeError('Synthetic target reboot after durable completion')
            reply = encrypt_mcp(b'\x00\x80\x73\x06\xff' + bytes([command.payload[4]]) + b'\x01\0\0\0',
                                base.KEY, len(self.frames), command.mcp_address)
            self.runtime.observe(reply, self.runtime.clock())
            return
        if packet[0] == 0:
            reply = envelope(b'\x01' + self.identity)
        else:
            self.association_starts += packet[0] == 2
            reply = self.peer.respond(frame)
            if packet[0] == 12 and packet[5] == 0 and self.disconnect_at == 'overflow_before_ack':
                # Genuine overflow: 65 DISTINCT authenticated replies for the
                # current network-key chunk. They are not stale or repeats, so
                # they fill the bounded queue and the exchange must fail closed.
                decoded = open_envelope(reply)
                for counter in range(65):
                    self.runtime.observe(envelope(decoded[:6] + bytes([counter])), self.runtime.clock())
                self.runtime.observe(reply, self.runtime.clock())
                return
            if packet[0] == 12 and packet[5] == 1:
                if self.disconnect_at == 'stale_final_ack':
                    self.boundary()
                    self.ready = self.runtime.mesh_ready = True
                    self.runtime.observe(reply, self.runtime.clock())
                    return
                if self.disconnect_at in ('stale_with_ack', 'stale_without_ack'):
                    # Correlated previous-chunk ACKs can never complete chunk1.
                    # They are dropped before the bounded queue, so they can
                    # neither hide nor fabricate the final-ACK checkpoint.
                    duplicate = envelope(open_envelope(reply)[:5] + b'\x00')
                    for _ in range(65):
                        self.runtime.observe(duplicate, self.runtime.clock())
                    if self.disconnect_at == 'stale_with_ack':
                        self.runtime.observe(reply, self.runtime.clock())
                    self.boundary()
                    return
                if self.disconnect_at in ('noise_with_ack', 'noise_without_ack'):
                    for _ in range(65):
                        self.runtime.observe(envelope(b'\x01' + self.identity), self.runtime.clock())
                    if self.disconnect_at == 'noise_with_ack':
                        self.runtime.observe(reply, self.runtime.clock())
                    self.boundary()
                    return
                if self.disconnect_at == 'without_final_ack':
                    self.boundary()
                    return
                if self.disconnect_at in ('after_final_ack', 'duplicate_final_ack', 'foreign_final_ack', 'corrupt_final_ack'):
                    if self.disconnect_at == 'duplicate_final_ack':
                        reply = envelope(open_envelope(reply)[:5] + b'\x00')
                    elif self.disconnect_at == 'foreign_final_ack':
                        decoded = bytearray(open_envelope(reply)); decoded[1] ^= 1
                        reply = envelope(bytes(decoded))
                    elif self.disconnect_at == 'corrupt_final_ack':
                        damaged = bytearray(reply); damaged[-2] ^= 1
                        reply = bytes(damaged)
                    # Deliver via the real raw-observer seam, then invalidate
                    # before the async queue consumer can run.
                    self.runtime.observe(reply, self.runtime.clock())
                    self.final_ack_delivered = True
                    self.boundary()
                    return
        self.runtime.observe(reply, self.runtime.clock())


class BootstrapRelayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ClaimTests.asyncSetUp
    asyncTearDown = base.ClaimTests.asyncTearDown

    async def interrupted_claim(self, mode):
        self.transport = BootstrapTransport(self.runtime, mode)
        self.runtime.transport = self.transport
        await self.claims.discover(seconds=0)
        with self.assertRaises(RuntimeError):
            await self.claims.claim(base.IDENTITY.hex(), 'Synthetic direct RL4', 93, inventory_complete=True)
        receipt, = self.store.journals()
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertEqual(self.store.inventory()['devices'], [])
        self.assertEqual(self.transport.association_starts, 1)
        self.assertEqual(self.transport.peer.target, receipt['target'])
        return receipt

    def reconnect_same_target(self):
        self.transport.disconnect_at = None
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()

    async def assert_ping_only_reconcile(self, receipt):
        self.reconnect_same_target()
        before = len(self.transport.frames)
        result = await self.claims.reconcile(receipt['id'])
        self.assertTrue(result['verified'])
        self.assertFalse(result['association_retried'])
        self.assertEqual(len(self.transport.frames), before + 1)
        with self.assertRaises(ValueError):
            open_envelope(self.transport.frames[-1])
        self.assertEqual(self.transport.association_starts, 1)
        self.assertEqual(self.store.journals()[0]['status'], 'verified')
        self.assertEqual(len(self.store.inventory()['devices']), 1)

    async def test_completed_checkpoint_survives_target_reboot_and_ping_only_reconciles(self):
        receipt = await self.interrupted_claim('after_checkpoint')
        self.assertTrue(receipt['detail']['association_complete'])
        await self.assert_ping_only_reconcile(receipt)

    async def test_observed_final_ack_is_checkpointed_before_synchronous_session_boundary(self):
        receipt = await self.interrupted_claim('after_final_ack')
        self.assertTrue(self.transport.final_ack_delivered)
        self.assertTrue(receipt['detail']['association_complete'])
        await self.assert_ping_only_reconcile(receipt)

    async def test_missing_final_ack_blocks_ping_proof_and_reclaim(self):
        receipt = await self.interrupted_claim('without_final_ack')
        self.assertFalse(receipt['detail']['association_complete'])
        self.reconnect_same_target()
        before = len(self.transport.frames)
        with self.assertRaises(ValueError):
            await self.claims.reconcile(receipt['id'])
        with self.assertRaises(ValueError):
            await self.claims.claim(base.IDENTITY.hex(), 'Do not retry', 93, inventory_complete=True)
        self.assertEqual(len(self.transport.frames), before)
        self.assertEqual(self.transport.association_starts, 1)
        self.assertEqual(self.store.journals()[0]['status'], 'uncertain')

    async def test_invalid_or_duplicate_final_ack_does_not_create_completion_checkpoint(self):
        # A separate store/peer per case preserves independent first-device state.
        for mode in ('duplicate_final_ack', 'foreign_final_ack', 'corrupt_final_ack'):
            with self.subTest(mode=mode):
                await self.asyncTearDown()
                await self.asyncSetUp()
                receipt = await self.interrupted_claim(mode)
                self.assertFalse(receipt['detail']['association_complete'])
                self.reconnect_same_target()
                before = len(self.transport.frames)
                with self.assertRaises(ValueError):
                    await self.claims.reconcile(receipt['id'])
                self.assertEqual(len(self.transport.frames), before)

    async def test_stale_generation_final_ack_cannot_authorize_recovery(self):
        receipt = await self.interrupted_claim('stale_final_ack')
        self.assertFalse(receipt['detail']['association_complete'])
        self.reconnect_same_target()
        with self.assertRaises(ValueError):
            await self.claims.reconcile(receipt['id'])

    async def test_genuine_overflow_before_final_ack_cannot_authorize_recovery(self):
        receipt = await self.interrupted_claim('overflow_before_ack')
        self.assertFalse(receipt['detail']['association_complete'])
        self.assertEqual(receipt['detail']['failure_code'], 'association_receive_overflow')
        observation = receipt['detail']['failure_observation']
        self.assertTrue(observation['queue_overflow'])
        # Earlier steps' replies are counted too; at least the full queue.
        self.assertGreaterEqual(observation['queued_target_frames'], 64)
        self.reconnect_same_target()
        before = len(self.transport.frames)
        with self.assertRaises(ValueError):
            await self.claims.reconcile(receipt['id'])
        self.assertEqual(len(self.transport.frames), before)

    async def test_stale_previous_chunk_flood_cannot_hide_or_fabricate_final_ack(self):
        for mode in ('stale_with_ack', 'stale_without_ack'):
            with self.subTest(mode=mode):
                await self.asyncTearDown()
                await self.asyncSetUp()
                receipt = await self.interrupted_claim(mode)
                self.assertEqual(receipt['detail']['association_complete'], mode == 'stale_with_ack')
                observation = receipt['detail']['failure_observation']
                self.assertFalse(observation['queue_overflow'])
                self.assertEqual(observation['out_of_step_frames'], 65)
                if mode == 'stale_with_ack':
                    await self.assert_ping_only_reconcile(receipt)
                else:
                    self.reconnect_same_target()
                    before = len(self.transport.frames)
                    with self.assertRaises(ValueError):
                        await self.claims.reconcile(receipt['id'])
                    self.assertEqual(len(self.transport.frames), before)

    async def test_ordinary_no_disconnect_claim_remains_verified(self):
        self.transport = BootstrapTransport(self.runtime, None)
        self.runtime.transport = self.transport
        await self.claims.discover(seconds=0)
        result = await self.claims.claim(base.IDENTITY.hex(), 'Ordinary', 93, inventory_complete=True)
        self.assertTrue(result['associated'])
        receipt, = self.store.journals()
        self.assertEqual(receipt['status'], 'verified')
        self.assertFalse(receipt['detail']['direct_bootstrap'])
        self.assertEqual(self.transport.association_starts, 1)
        self.assertEqual(self.transport.pings, 1)

    async def test_unrelated_noise_cannot_hide_or_fabricate_final_ack_checkpoint(self):
        for mode in ('noise_with_ack', 'noise_without_ack'):
            with self.subTest(mode=mode):
                await self.asyncTearDown()
                await self.asyncSetUp()
                receipt = await self.interrupted_claim(mode)
                self.assertEqual(receipt['detail']['association_complete'], mode == 'noise_with_ack')
                self.assertFalse(receipt['detail']['failure_observation']['queue_overflow'])
                self.assertEqual(receipt['detail']['failure_observation']['ignored_frames'], 65)
                if mode == 'noise_with_ack':
                    await self.assert_ping_only_reconcile(receipt)
                else:
                    self.reconnect_same_target()
                    with self.assertRaises(ValueError):
                        await self.claims.reconcile(receipt['id'])

    async def test_bootstrap_identity_gate_rejects_before_any_io_or_receipt(self):
        identity = bytes.fromhex('000102030405140000040a0b0c0d0e0f')
        selected = ':'.join(f'{value:02X}' for value in identity[:6][::-1])
        self.transport = BootstrapTransport(self.runtime, None, identity)
        self.runtime.transport = self.transport
        cases = [(None, identity, 93), ('00:00:00:00:00:01', identity, 93),
                 (selected, identity, 127), (selected, base.IDENTITY, 93)]
        for address, candidate, product in cases:
            with self.subTest(address=address, candidate=candidate.hex(), product=product):
                self.runtime.bootstrap = SimpleNamespace(needs_first_claim=True, selected_address=address)
                with self.assertRaises(ValueError):
                    await self.claims.claim(candidate.hex(), 'Rejected', product, inventory_complete=True, refresh_identity=True)
                self.assertEqual(self.transport.frames, [])
                self.assertEqual(self.store.journals(), [])

    async def test_bootstrap_matching_rl4_commits_direct_provenance(self):
        identity = bytes.fromhex('000102030405140000040a0b0c0d0e0f')
        selected = ':'.join(f'{value:02X}' for value in identity[:6][::-1])
        self.runtime.bootstrap = SimpleNamespace(needs_first_claim=True, selected_address=selected)
        self.transport = BootstrapTransport(self.runtime, None, identity)
        self.runtime.transport = self.transport
        await self.claims.discover(seconds=0)
        result = await self.claims.claim(identity.hex(), 'First direct RL4', 93, inventory_complete=True)
        self.assertTrue(result['associated'])
        receipt, = self.store.journals()
        self.assertEqual(receipt['status'], 'verified')
        self.assertTrue(receipt['detail']['direct_bootstrap'])
        self.assertEqual(receipt['detail']['bootstrap_address'], selected)
        self.assertEqual(self.transport.association_starts, 1)
        self.assertEqual(self.transport.pings, 1)
