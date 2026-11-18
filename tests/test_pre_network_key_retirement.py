"""Synthetic RL4 retirement after the proposed address but before the network key.

Receipts come from the real claim engine and an independent ECDH Peer. A light that
never received the network key cannot be a member of this mesh; recovery still needs
an isolated worker and a fresh exact unpaired observation, retires the address for
good and sends no reset, pairing or retry. Network-key phases stay excluded.
"""
import asyncio
import copy
import unittest
from unittest.mock import patch

import test_claim_development as base
import test_public_key_retirement as pk
from avionmesh_pi import commissioning, identity
from avionmesh_pi.association import open_envelope
from avionmesh_pi.management import recovery_advice

FINGERPRINT = pk.FINGERPRINT


def receipt(phase, attempted, accepted=None):
    value = pk.public_key_receipt()
    value['detail'].update(association_phase=phase, submissions_attempted=attempted,
                           submissions_accepted=attempted if accepted is None else accepted)
    return value


class PreNetworkKeyProfileTests(unittest.TestCase):
    def test_exact_phase_and_count_pairs_only(self):
        for phase, count in (('confirm', 8), ('random', 9), ('device_id', 10)):
            with self.subTest(phase=phase):
                self.assertTrue(identity.rl4_pre_network_key_claim_profile(receipt(phase, count)))
                self.assertFalse(identity.rl4_public_key_claim_profile(receipt(phase, count)))
        for phase, count, accepted in (('device_id', 9, None), ('device_id', 11, None), ('device_id', 10, 9),
                                       ('random', 10, None), ('confirm', 7, None), ('network_key', 11, None),
                                       ('network_key', 12, None), ('public_key', 7, None), ('complete', 12, None),
                                       ('device_id', True, None), ('device_id', 10.0, None)):
            with self.subTest(phase=phase, count=count, accepted=accepted):
                self.assertFalse(identity.rl4_pre_network_key_claim_profile(receipt(phase, count, accepted)))

    def test_same_identity_and_receipt_integrity_rules(self):
        for section, field, value in pk.invalid_profiles():
            if field in ('association_phase', 'submissions_attempted', 'submissions_accepted'):
                continue
            candidate = receipt('device_id', 10)
            target = candidate if section == 'journal' else candidate['detail']
            if value is pk._MISSING:
                del target[field]
            else:
                target[field] = value
            with self.subTest(section=section, field=field, value=value):
                self.assertFalse(identity.rl4_pre_network_key_claim_profile(candidate))

    def test_recovery_advice_names_the_new_action(self):
        advice = recovery_advice(receipt('device_id', 10))
        self.assertEqual(advice['actions'], ['retire_pre_network_key_claim'])
        self.assertIs(advice['retry_allowed'], False)
        self.assertEqual(recovery_advice(receipt('network_key', 11))['actions'], [])


class PreNetworkKeyRetirementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pk.PublicKeyRetirementTests.asyncSetUp
    asyncTearDown = pk.PublicKeyRetirementTests.asyncTearDown
    ready_again = pk.PublicKeyRetirementTests.ready_again
    assert_only_discovery = pk.PublicKeyRetirementTests.assert_only_discovery
    assert_not_retired = pk.PublicKeyRetirementTests.assert_not_retired

    async def failed_claim(self, opcode, chunk=None):
        """The light accepts the request but its reply is lost at ``opcode``."""
        self.transport.peers[FINGERPRINT] = pk.Peer()
        await self.claims.discover()
        send = self.transport.send
        wait_for = asyncio.wait_for

        async def lose_reply(frame):
            try:
                payload = open_envelope(frame)
            except ValueError:  # encrypted MCP (Ping), not association
                return await send(frame)
            if payload[0] == opcode and (chunk is None or payload[5] == chunk):
                self.transport.frames.append(frame)
                self.transport.peers[FINGERPRINT].respond(frame)
                return
            await send(frame)

        async def bounded(awaitable, timeout):
            fast = timeout in (commissioning.REPLY_WAIT, commissioning.SLOW_REPLY_WAIT)
            return await wait_for(awaitable, 0.05 if fast else min(timeout, 1))

        with patch.object(self.transport, 'send', side_effect=lose_reply):
            with patch('avionmesh_pi.commissioning.asyncio.wait_for', side_effect=bounded):
                with self.assertRaises(asyncio.TimeoutError):
                    await self.claims.claim(pk.base.IDENTITY, 'Interrupted RL4', 93, inventory_complete=True)
        failed, = self.store.journals(pending=True)
        self.assertIn(failed['id'], self.runtime.claim_isolated)
        if opcode == 10:  # stopped before any network-key request
            self.assertFalse(self.transport.peers[FINGERPRINT].netkey)
        self.ready_again()
        return failed

    async def test_lost_device_id_reply_retires_without_reset_or_retry(self):
        failed = await self.failed_claim(10)
        detail = failed['detail']
        self.assertEqual((detail['association_phase'], detail['submissions_attempted'],
                          detail['submissions_accepted']), ('device_id', 10, 10))
        self.assertEqual(detail['failure_code'], 'association_response_timeout')
        self.assertEqual(self.transport.peers[FINGERPRINT].target, failed['target'])  # address was proposed
        before = len(self.transport.frames)
        result = await self.claims.reconcile_pre_key(failed['id'])
        self.assertTrue(result['pre_key_retired'])
        self.assertFalse(result['association_retried'])
        self.assertFalse(result['automatic_reclaim'])
        self.assert_only_discovery(before)
        proof, = [j for j in self.store.journals() if j['kind'] == 'claim_pre_key']
        self.assertEqual(proof['detail']['recovery_schema'], 'claim-pre-network-key-retirement-v1')
        self.assertIs(proof['detail']['device_id_dispatched'], True)
        self.assertIs(proof['detail']['network_key_dispatched'], False)
        self.assertEqual(proof['detail']['original_receipt'], failed)
        self.assertIn(failed['target'], self.store.get('retired_addresses'))
        retired = next(j for j in self.store.journals() if j['id'] == failed['id'])
        self.assertEqual(retired['status'], 'retired')
        self.assertFalse(self.store.journals(pending=True))

    async def test_later_claim_uses_a_new_address(self):
        failed = await self.failed_claim(10)
        await self.claims.reconcile_pre_key(failed['id'])
        self.ready_again()
        self.transport.peers[FINGERPRINT] = pk.Peer()
        await self.claims.discover()
        result = await self.claims.claim(pk.base.IDENTITY, 'Second try', 93, inventory_complete=True)
        self.assertTrue(result['associated'])
        self.assertGreater(result['device_id'], failed['target'])

    async def test_lost_network_key_reply_stays_excluded(self):
        failed = await self.failed_claim(12, chunk=0)
        self.assertEqual(failed['detail']['association_phase'], 'network_key')
        self.assertEqual(recovery_advice(failed)['actions'], [])
        before = len(self.transport.frames)
        with self.assertRaises(ValueError):
            await self.claims.reconcile_pre_key(failed['id'])
        self.assertEqual(len(self.transport.frames), before)
        self.assert_not_retired(failed)

    async def test_unisolated_or_unobserved_attempt_is_not_retired(self):
        failed = await self.failed_claim(10)
        self.runtime.claim_isolated.discard(failed['id'])
        with self.assertRaises(ValueError):
            await self.claims.reconcile_pre_key(failed['id'])
        self.assert_not_retired(failed)
        self.runtime.claim_isolated.add(failed['id'])
        self.transport.ads = [ad for ad in self.transport.ads if ad != pk.base.IDENTITY]
        result = await self.claims.reconcile_pre_key(failed['id'])
        self.assertFalse(result['retired'])
        self.assert_not_retired(failed)


class SlowAddressReplyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pk.PublicKeyRetirementTests.asyncSetUp
    asyncTearDown = pk.PublicKeyRetirementTests.asyncTearDown

    async def claim_with_slow_device_id_reply(self, delay):
        self.transport.peers[FINGERPRINT] = pk.Peer()
        await self.claims.discover()
        send = self.transport.send

        async def slow_reply(frame):
            try:
                payload = open_envelope(frame)
            except ValueError:  # encrypted MCP (Ping), not association
                return await send(frame)
            if payload[0] != 10:
                return await send(frame)
            self.transport.frames.append(frame)
            reply = self.transport.peers[FINGERPRINT].respond(frame)
            asyncio.get_running_loop().call_later(delay, lambda: self.runtime.observe(reply, self.runtime.clock()))

        with patch.object(self.transport, 'send', side_effect=slow_reply), \
             patch.object(commissioning, 'REPLY_WAIT', 0.2), patch.object(commissioning, 'SLOW_REPLY_WAIT', 1.0):
            return await self.claims.claim(pk.base.IDENTITY, 'Slow RL4', 93, inventory_complete=True)

    async def test_slow_device_id_reply_now_completes(self):
        # 0.5 s is longer than the ordinary per-reply wait (0.2 s here) but within the
        # longer address/key wait, so the pairing completes instead of stalling.
        result = await self.claim_with_slow_device_id_reply(0.5)
        self.assertTrue(result['associated'])
        self.assertTrue(self.transport.peers[FINGERPRINT].netkey)
        self.assertEqual(self.transport.starts.count(FINGERPRINT), 1)  # one association, no retry

    def test_wait_constants(self):
        self.assertEqual((commissioning.REPLY_WAIT, commissioning.SLOW_REPLY_WAIT,
                          commissioning.ASSOCIATION_DEADLINE), (5, 15, 60))
