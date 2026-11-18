"""DeviceID ack stays strict: it must carry the target's UUID hash.

0.5.0.dev8 briefly accepted an authenticated DeviceID ack by opcode alone (ESP
parity). Six live RL4 claims afterwards all answered with a matching hash, so that
relaxation was reverted. Each claim still records device_id_ack_hash_matched.
Synthetic Peer only.
"""
import unittest
from unittest.mock import patch

import test_public_key_retirement as pk
from avionmesh_pi.association import Association, envelope, open_envelope
from test_association import IDENTITY, KEY, Peer

FINGERPRINT = pk.FINGERPRINT


def advance_to(protocol, peer, opcode):
    """Run a real exchange until the bridge's next request has ``opcode``."""
    request = protocol.start()
    while open_envelope(request)[0] != opcode:
        request = protocol.process(peer.respond(request))
    return request


def without_hash(reply, filler=bytes(4)):
    p = open_envelope(reply)
    return envelope(p[:1] + filler + p[5:])


class DeviceIdAckProtocolTests(unittest.TestCase):
    def test_hashless_device_id_ack_is_ignored(self):
        for reply in (lambda r: without_hash(r), lambda r: without_hash(r, b'\xff' * 4), lambda r: envelope(b'\x0b')):
            with self.subTest(reply=reply):
                protocol, peer = Association(IDENTITY, 40001, KEY), Peer()
                request = advance_to(protocol, peer, 10)
                self.assertIsNone(protocol.process(reply(peer.respond(request))))
                self.assertEqual(protocol.state, 'device_id')
                self.assertIsNone(protocol.device_id_ack_hash_matched)

    def test_matching_ack_advances_and_is_recorded(self):
        protocol, peer = Association(IDENTITY, 40001, KEY), Peer()
        request = advance_to(protocol, peer, 10)
        request = protocol.process(peer.respond(request))
        self.assertIs(protocol.device_id_ack_hash_matched, True)
        for _ in range(2):  # two network-key chunks, each acknowledged with the hash
            request = protocol.process(peer.respond(request))
        self.assertEqual(protocol.state, 'complete')
        self.assertTrue(peer.netkey)

    def test_network_key_acks_still_require_the_exact_hash(self):
        protocol, peer = Association(IDENTITY, 40001, KEY), Peer()
        request = advance_to(protocol, peer, 12)
        self.assertIsNone(protocol.process(without_hash(peer.respond(request))))
        self.assertEqual((protocol.state, protocol.chunk), ('network_key', 0))


class DeviceIdAckClaimTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pk.PublicKeyRetirementTests.asyncSetUp
    asyncTearDown = pk.PublicKeyRetirementTests.asyncTearDown

    async def test_normal_claim_records_matched_hash(self):
        self.transport.peers[FINGERPRINT] = Peer()
        await self.claims.discover()
        result = await self.claims.claim(pk.base.IDENTITY, 'Strict RL4', 93, inventory_complete=True)
        self.assertTrue(result['associated'])
        receipt, = [j for j in self.store.journals() if j['kind'] == 'claim']
        self.assertIs(receipt['detail']['device_id_ack_hash_matched'], True)
        self.assertIn('relay_address', receipt['detail'])
