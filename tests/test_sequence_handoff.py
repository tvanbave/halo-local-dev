"""Synthetic security evidence; never admits or transmits to an existing mesh."""
import unittest

from avionmesh_pi.protocol import derive_mcp_key, encrypt_mcp, decrypt_mcp


class SequenceHandoffEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.key = derive_mcp_key('synthetic-sequence-investigation')
        self.a = bytes.fromhex('019073000a0000000100000000')
        self.b = bytes.fromhex('029073000a000000fe00000000')

    def test_same_key_source_sequence_reuses_ofb_stream_across_destinations(self):
        a = encrypt_mcp(self.a, self.key, 9)
        b = encrypt_mcp(self.b, self.key, 9)
        xor = lambda x, y: bytes(i ^ j for i, j in zip(x, y))
        self.assertEqual(xor(a[5:-9], b[5:-9]), xor(self.a, self.b))
        self.assertEqual(decrypt_mcp(a, self.key).crypto_source, 32768)
        self.assertNotEqual(decrypt_mcp(a, self.key).mcp_address,
                            decrypt_mcp(b, self.key).mcp_address)

    def test_authentication_still_rejects_tampering_despite_reused_stream(self):
        frame = bytearray(encrypt_mcp(self.a, self.key, 9))
        frame[8] ^= 1
        with self.assertRaisesRegex(ValueError, 'authentication'):
            decrypt_mcp(bytes(frame), self.key)

    def test_crypto_decoder_is_not_a_replay_filter(self):
        frame = encrypt_mcp(self.a, self.key, 9)
        self.assertEqual(decrypt_mcp(frame, self.key), decrypt_mcp(frame, self.key))

    def test_changing_source_or_key_changes_iv_domain_in_encoder_only(self):
        frame = encrypt_mcp(self.a, self.key, 9)
        other_source = encrypt_mcp(self.a, self.key, 9, source=0x8001)
        other_key = encrypt_mcp(self.a, derive_mcp_key('synthetic-distinct-mesh'), 9)
        self.assertNotEqual(frame[5:-9], other_source[5:-9])
        self.assertNotEqual(frame[5:-9], other_key[5:-9])
        self.assertEqual(decrypt_mcp(other_source, self.key).crypto_source, 0x8001)
        # This proves software encoding, not source allocation or firmware acceptance.

    def test_partial_observed_maximum_does_not_prove_unused_future(self):
        history = [3, 9, 5]
        observed = [3, 5]  # capture missed the middle send
        proposed = list(range(max(observed) + 1, 11))
        self.assertIn(9, set(history) & set(proposed))

    def test_legacy_low_byte_or_one_excludes_all_even_values(self):
        support = {byte | 1 for byte in range(256)}
        self.assertEqual(len(support), 128)
        self.assertTrue(all(value & 1 for value in support))
        self.assertTrue(support.isdisjoint(range(0, 256, 2)))
        # No assertion that every historical vendor/bridge writer used this code.
