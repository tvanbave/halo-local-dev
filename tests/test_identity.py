import unittest
from avionmesh_pi.identity import advertised_identity,paired_identity
from avionmesh_pi.protocol import Packet,ping_match

class IdentityTests(unittest.TestCase):
    def test_uuid_mapping_is_only_advisory(self):
        raw = bytearray(16);raw[6]=20;raw[9]=7
        result = advertised_identity(raw.hex())
        self.assertEqual(result['candidate_model'],91)
        self.assertFalse(result['model_verified'])
    def test_paired_codes_are_distinct_and_short_signature_needs_context(self):
        result = paired_identity(bytes([6,255,1,1,2,3,0,0,26,4]))
        self.assertEqual(result['candidate_model'],127)
        short = bytes([6,255,1,1,10,7,3])
        self.assertIsNone(paired_identity(short)['candidate_model'])
        self.assertEqual(paired_identity(short,True)['candidate_model'],91)
        with self.assertRaises(ValueError):paired_identity(bytes([6,255,0,0,1,1,0,0,0,0]))
    def test_ping_matches_real_short_and_long_layouts_only(self):
        for payload in (bytes([6,255,1,1,10,7,3]),bytes([6,255,1,1,2,3,0,0,26,4])):
            self.assertTrue(ping_match(Packet(1,40001,32768,115,payload),40001,1))
            self.assertFalse(ping_match(Packet(1,40002,32768,115,payload),40001,1))
            self.assertFalse(ping_match(Packet(1,40001,123,115,payload),40001,1))
